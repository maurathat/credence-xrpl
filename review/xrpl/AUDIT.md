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

---

# Checkpoint 1R-B: E3/E4 evidence audit (implementer commit `44fb089`)

Read-only audit of `44fb089f0cb4281608ab0b9a67e129147f349887`. Nothing under `spike/xrpl/` was
modified. No transaction was submitted, no `xrpld` was started, and E5/E6 were not audited.
All checks below were recomputed by the reviewer from committed evidence (raw submit responses,
validated `tx` responses with metadata, ledger snapshots, and the close log). The implementer's
assertion fields and `REPORT.md` were not relied on.

## R1. Independence record

`review/xrpl/EXPECTATIONS.md` sha256 `4ee2eb94b161232474942c02f764f5b7f4d29485fedfd0f2de9c4a681dfd81ee`,
identical to the Phase-A hash recorded at 2026-10-11T07:18:46Z. Its only commit is `89fa0a6`, and
`git diff 89fa0a6 -- review/xrpl/EXPECTATIONS.md` is empty. Not edited.

## R2. Implementer history (`d4eb874` to `44fb089`)

| Commit | What it is | Protocol evidence? |
| --- | --- | --- |
| `9408965` | 1B: E1 regression stop | no |
| `38d7c57` | 1B-R: Amendments ledger-entry validation fix, `start_ledger.sh` | E1 re-validation |
| `b366efe` | 1C: harness stop during E3 (15 txs, run discarded as evidence) | historical |
| `546e395` | 1C-R: harness and runner, partial E3/E4 | historical |
| `42845ca` | 1C-H: **`STATE-ASSERTIONS.md` (A-K oracle) written**, harness hardened, H1-H12 | no (zero tx) |
| `377d994` | 1C-F: stop; runner did not encode the full oracle | no (zero tx) |
| `5369ff0` | 1C-B: oracle bound to runner (`frozen_oracle.py`, `oracle_fixture.py`, runner), B1-B12, synthetic dry run | no (synthetic only) |
| `44fb089` | 1C-X: **live run**. Touches only `REPORT.md`, `RESULTS.json`, `evidence/checkpoint-1c-x/` | **yes** |

Only `evidence/checkpoint-1c-x/` is final protocol evidence. Checkpoints 1C and 1C-R are harness
development history and are not used below. The 1c-x ledger chain shares no ledger hash with 1c-r.

## R3. Frozen-oracle audit

Sha256 recomputed with `git show <commit>:<path>`:

| File | 42845ca | 377d994 | 5369ff0 | 44fb089 |
| --- | --- | --- | --- | --- |
| `STATE-ASSERTIONS.md` | `836b90b7…416a` | same | same | same |
| `scripts/harness.py` (semantic helpers) | `098bc81f…e7b` | same | same | same |
| `tests/test_state_assertions.py` (H1-H12) | `634880c3…b77c` | same | same | same |
| `scripts/frozen_oracle.py` (bindings, expected results, classification) | absent | absent | `bfa00b41…b2be` | same |
| `tests/test_frozen_oracle_runner.py` (B1-B12) | absent | absent | `935e53b4…e37e` | same |
| `scripts/oracle_fixture.py` | absent | absent | `d8c7af4e…11a7` | same |
| `scripts/run_checkpoint_1c_r.py` | `c2787b13…ff67` | same | `f0f68971…693f` | same |

- The oracle, H1-H12 and the semantic helpers have not changed since `42845ca`. The bindings,
  expected results, classification semantics and B1-B12 were fixed at `5369ff0` and are unchanged
  in `44fb089`. All hashes the REPORT claims match.
- Timeline: `5369ff0` committed 22:22:00Z; the fresh server reported `uptime 5` at
  22:24:23Z (`environment-start.json`); `44fb089` committed 22:28:44Z. The live run sits between
  the frozen commit and the evidence commit.
- Order: `oracle-a-k.json` records `A B C D E F G H I J K`, each exactly once. Included
  transaction ledger indices increase monotonically (13, 14, 15, 223, 226, 227). The close log is
  one unbroken hash chain from genesis ledger 2 to 228 (226 closes, all `success`).
- Zero stops/retries in the final run: one ledger chain, each step once, every account's sequence
  contiguous (D and E reuse lender sequence 7, which F then consumes; G and K hold the next unused
  sequence). No duplicate submission of any step exists in the evidence.
- 64 spike tests (H1-H12, B1-B12 and helpers) pass on the reviewer's export.
- **Condition C1:** the run evidence carries no hash of the oracle or runner it executed. Binding
  rests on git history plus the timeline above. An uncommitted, edited-then-reverted runner cannot
  be excluded from the evidence alone. Nothing suggests one.

## R4. E3 A-G (vault `B87C0BAC…5C1B`)

Recomputed from validated metadata (`AffectedNodes`) and submit responses:

| Step | Tx / acct seq | Submit | Validated | Ledger (close) | Governing clock* | Recomputed state effect |
| --- | --- | --- | --- | --- | --- | --- |
| A VaultCreate | `2604E1A3…` / owner 7 | tesSUCCESS | VALIDATED tesSUCCESS | 13 (845331871) | 845331870 | Created Vault: Owner = loan_broker actor `rHTUQ57n…`, Asset tRLUSD/issuer `rUQwCP6P…`, VaultKind 1, Sub 845331900, Red 845332080, policy 1, Flags 0. Created pseudo `rECkRrRX…`, MPTokenIssuance (Issuer = pseudo, AssetScale 6), owner MPToken for `ShareMPTID`, pseudo trust line. Owner OwnerCount 1→4, XRP −2,000,000 drops (VaultCreate fee) |
| B deposit 100000 | `CA6EFADF…` / lender 5 | tesSUCCESS | VALIDATED | 14 (845331872) | 845331871 | lender line 200000→100000; pseudo line 0→100000; AssetsTotal and AssetsAvailable 0→100000 (Final); Outstanding 0→100000000000; lender MPToken created 100000000000 |
| C withdraw 1000 | `949595F0…` / lender 6 | tesSUCCESS | VALIDATED | 15 (845331873) | 845331872 | Assets 100000→99000; lender 100000→101000; shares 100000000000→99000000000 (issuance and holder) |
| D deposit 1 | `98A0D908…` / lender 7 | **tecEXPIRED**, applied=false, kept=false | NOT_IN_VALIDATED_LEDGER (LLS 63 < 228, cannot be included) | evaluated on open ledger over 43 | 845331901 | Protected set byte-identical pre/post, including `PreviousTxnID` of Vault, issuance and MPToken (`949595F0`, i.e. C) and lender sequence 7. No fee charged (lender XRP unchanged until F) |
| E withdraw 1 | `C4ADAF5D…` / lender 7 | **tecTOO_SOON**, applied=false | NOT_IN_VALIDATED_LEDGER (LLS 64) | over 44 | 845331902 | identical, as D |
| F withdraw 1000 | `E506509E…` / lender 7 | tesSUCCESS | VALIDATED | 223 (845332081) | **845332080 = RedemptionDate** | Assets 99000→98000; lender 101000→102000; shares 99000000000→98000000000 |
| G deposit 1 | `B741566B…` / lender 8 | **tecEXPIRED**, applied=false | NOT_IN_VALIDATED_LEDGER (LLS 243, kept=false) | over 223 | 845332081 | Protected set identical, `PreviousTxnID` = F |

\* Governing clock = close time of the parent ledger of the evaluating view (rippled
`hasExpired` uses `parentCloseTime`; EXPECTATIONS Q4).

- Negative results were not overwritten. Each record keeps `submission_result` and
  `authoritative_result` equal to the engine result, with `validated_lookup_status` reported
  separately. The post-negative `tx` lookups return `validated: false` with no metadata.
- **Condition C2:** the negatives were submitted with `fail_hard`, so `tec` results were not
  applied or kept. In a normal submission, rippled includes a `tec` result in a ledger and charges
  the fee and sequence. The evidence shows the phase rejection, not that inclusion behaviour.
- **Residual R-G/K:** G and K still had open `LastLedgerSequence` windows when evidence ended
  (243 and 247, last ledger 228). Inclusion is ruled out by `kept=false` under `fail_hard`, not by
  window expiry.

## R5. Phase timing (independent reconstruction)

- SubscriptionDate 845331900, RedemptionDate 845332080; gap 180 s = `kMinInvestmentPeriod`.
- Every one of the 226 closes advanced close time by exactly **1 s** (each close sets
  `max(round(wall clock), parent + 1 s)`). Ledger time was therefore driven by the close count.
- Boundary ledgers: L41 845331899 and L42 845331900 (= Sub, still Subscription, inclusive); L43
  845331901 (first Investment parent). L221 845332079 (Investment); L222 845332080 (= Red,
  Redemption, inclusive).
- B/C parents 845331871/845331872 ≤ Sub, so **Subscription**. D/E parents 845331901/845331902,
  strictly after Sub and before Red, so **Investment**. F/G parents 845332080/845332081 ≥ Red, so
  **Redemption**. F was evaluated exactly at the RedemptionDate boundary and D one second after the
  SubscriptionDate boundary. These are the tightest possible boundary tests, and no off-by-one was
  found.
- Close count: 21 validation/absence closes + 28 (L15→L43) + 177 (L45→L222) = 226.
- Conclusions use ledger close time only. Wall clock enters only through the 1 s-per-close rule.

## R6. E3 final state

Recomputed by chaining metadata deltas: AssetsTotal = AssetsAvailable = 0 + 100000 − 1000 − 1000
= **98000**. Lender tRLUSD = 200000 − 100000 + 1000 + 1000 = **102000**. Shares: issuance
`OutstandingAmount` = lender `MPTAmount` = **98000000000** (= 98000 × 10^6, AssetScale 6, share
price 1). This matches the G-time protected snapshot and the K-time owner object set. **No
LoanSet**: the only TransactionTypes in `checkpoint-1c-x` are Payment, AccountSet, TrustSet,
VaultCreate, VaultDeposit, VaultWithdraw, LoanBrokerSet and LoanBrokerCoverDeposit.

**E3 classification: SUPPORTED** for deposit/withdraw phase gating on a closed-ended tRLUSD vault.
Scope limits: LoanSet phase gating is not tested; conditions C2 and C3 (R10) apply.

## R7. E4 H-K (vault `D17C5E01…08CA`, LoanBroker `220A5629…AEC8`)

- **Prerequisite:** a second closed-ended vault was created at L225 by the same owner
  (Sub 845332112, Red 845332292). H, J and K ran while that vault was in **Subscription**
  (parents 845332083..845332085). **Condition C3:** the broker was not attached to the E3 vault,
  which was funded and had been through every phase. The E4 vault holds no deposits. Source has no
  phase gate on `LoanBrokerSet` (EXPECTATIONS Q3), so this is a scope limit, not a defect.
- **H** `9C168D6A…`, owner seq 9: tesSUCCESS, VALIDATED at L226. The created LoanBroker has
  Owner = `rHTUQ57n…` (= Vault Owner), VaultID = `D17C5E01…`, pseudo Account `rJaqTJQW…`,
  DebtMaximum 50000, CoverRateMinimum 10000, CoverRateLiquidation 25000, ManagementFeeRate 0
  (default), Sequence 9. The broker pseudo AccountRoot and its tRLUSD line were created. Owner
  OwnerCount 7→9 (+2, as the source says). The Vault object was not modified.
- **I:** a semantic check that inherits H. LoanBroker.VaultID = Vault index; LoanBroker.Owner =
  Vault.Owner; Vault pseudo `rUWkezfE…` and broker pseudo `rJaqTJQW…` are both present and
  distinct (and distinct from the E3 vault pseudo `rECkRrRX…`).
- **J** `81F52D2D…`, owner seq 10: tesSUCCESS, VALIDATED at L227. Owner line 20000→10000; broker
  pseudo `rJaqTJQW…` line 0→10000 (the line created in H, so the cover went to the broker's pseudo,
  not the vault's); LoanBroker FinalFields CoverAvailable 10000; DebtTotal absent (default 0);
  rates and limits unchanged; `PreviousTxnID` = J.
- **K** `109CA7FE…`, submitted by the borrower actor `rHCFQpcm…` seq 6: **tecNO_PERMISSION**,
  applied=false, kept=false, NOT_IN_VALIDATED_LEDGER. The protected owner and non-owner object
  sets, balances and sequences are byte-identical pre/post. No LoanBroker owned by the non-owner
  exists. The source basis is `LoanBrokerSet` preclaim `account != vault Owner`.

**E4 classification: SUPPORTED.**

## R8. Cover state and classification

| Item | Value | Basis | Class |
| --- | --- | --- | --- |
| CoverAvailable | 10000 | J FinalFields; K snapshot | OBSERVED |
| Broker pseudo holding | 0 → 10000 | J RippleState `EC1F4301` | OBSERVED |
| Owner tRLUSD | 20000 → 10000 | J RippleState `CB053536` | OBSERVED |
| CoverRateMinimum | 10000 (10%) | H NewFields | OBSERVED |
| CoverRateLiquidation | 25000 (25%) | H NewFields | OBSERVED |
| DebtMaximum | 50000 | H NewFields | OBSERVED |
| DebtTotal | 0 (default; field absent) | H/J | OBSERVED |
| Default cover = min(DebtTotal × CRM × CRL, DefaultAmount, CoverAvailable) | n/a | source (`LoanManage.cpp` defaultLoan) | DERIVED |
| Default, loss, liquidation, clawback, lender loss | not executed | none | UNRESOLVED |

With these parameters the derived bound is 2.5% of DebtTotal (10% × 25%), capped by
CoverAvailable. **The plan's sentence "Loss absorbed by Credence's first-loss cover before
lenders lose anything" (line 249) still needs correction** (PLAN-DIFF P22).

## R9. Evidence integrity

- `checkpoint-1c-x/MANIFEST.json`: 34 files, every sha256 and byte count matches. No file is on
  disk without being listed (only the manifest itself), and none is listed but missing. All 7
  evidence manifests in the tree verify (85 payload files).
- Every A-K record in `oracle-a-k.json` is byte-identical to its per-transaction file (I to its
  own file). No live record has `synthetic: true`. The synthetic dry run is isolated in
  `checkpoint-1c-b/` and labelled as such.
- Secrets: gitleaks v8.21.2 with the repo config over the exported spike tree and the full branch
  history (12 commits through `44fb089`) found nothing. Actor addresses are public; no seed or private key appears.
- Signing material: `public_transaction` removes `SigningPubKey`, `TxnSignature`,
  `CounterpartySignature` and blobs, but records the authorization mode, the multisign signer
  accounts and presence flags. For E3/E4 (single-signed by the named account) this is enough;
  the single-sign mode and `Account` field are recorded for every step. **Condition C4:** without
  signatures or blobs, the transaction hashes cannot be recomputed from the canonical form, and
  the standalone ledger is gone, so hash-to-content binding rests on the captured rippled responses.
  This partly addresses the earlier finding S2. For E5/E6, per-signer public keys or signed blobs
  should be retained.
- Sufficiency: every conclusion in R4 to R8 was reproduced from raw metadata, snapshots and the
  close log without using `REPORT.md` or the harness's assertion results.

## R10. False-positive search (committed evidence only)

| Hazard | Finding |
| --- | --- |
| Success without correct state | None. Every positive step's metadata deltas match the oracle predicates |
| Stale pre/post state | None. All targeted post-state objects carry the step's own hash as `PreviousTxnID`; no pre-state object does |
| Wrong vault/broker object | None. E3 consistently uses `B87C0BAC`; E4 consistently uses `D17C5E01`/`220A5629`; IDs match `CreatedNode` indices |
| Wrong phase / off-by-one | None (R5) |
| Auxiliary mistaken for target | None. Directory, pseudo-account and issuer threading nodes (e.g. issuer AccountRoot `60A778C1`, threading-only) are not used as targets |
| Negative overwritten or misclassified | None (R4) |
| Mutation during a negative | None. Byte-identical protected state, including `PreviousTxnID` |
| Wrong owner / broker account | None. Vault Owner = LoanBroker Owner = `loan_broker` actor; K uses the borrower actor |
| Cover into the wrong account | None. The credit went to broker pseudo `rJaqTJQW…`, not vault pseudo `rUWkezfE…` |
| Fixture or synthetic evidence used as live | None. The setup snapshot is live (L12); the synthetic dry run is only in 1c-b |
| Earlier run mixed in | None. Distinct genesis and zero shared ledger hashes with 1c-r |
| Later retry mistaken for the first frozen run | None found. One chain, one record per step (with condition C1) |

`"PreviousFields": {}` on the Vault (B) and LoanBroker (J) is native rippled output: the changed
fields were default-zero, so they are recorded but omitted from JSON. It is not an evidence edit.

## R11. Conclusion

E3 and E4 are supported by the committed evidence as claimed. Conditions C1 to C4 are bounded
scope and provenance limits; none falsifies a claim. **Verdict: PASS WITH CONDITIONS.** The
plan's first-loss wording still needs correction, and the R4 notes on C2 should be recorded in
any later report.
