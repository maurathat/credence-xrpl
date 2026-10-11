# XRPL protocol expectations (Phase A, pre-implementation record)

Reviewer: XRPL review workstream (Claude Code), independent of the spike.
Written before any implementer spike output was opened. This file is not edited
after the audit starts; later findings go in `AUDIT.md` and `PLAN-DIFF.md`.

Labels: **CONFIRMED** (read in the cited source and consistent with the docs),
**LIKELY** (follows from the source path, but no upstream test or run shows it),
**UNKNOWN**, **CONTRADICTED** (the protocol disagrees with the stated assumption).
"CONFIRMED" here means confirmed by reading source. It is not a run result. Every
item still needs the listed experiment on the pinned build.

## Evidence base

| Source | Identity |
| --- | --- |
| rippled 3.4.1 source | tag `3.4.1`, commit `00e640751460c137636fdb6dae6b4280de355d81`, GitHub release published 2026-10-10T00:04:48Z, not a prerelease. Tarball `XRPLF/rippled/archive/refs/tags/3.4.1.tar.gz`, sha256 `d195f4dbc2c12dc32449c30cd27343d8c66eccbe4335f86af7a04d04d683140e` |
| rippled 3.4.0 source | tag `3.4.0`, commit `4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`, release published 2026-09-17. Tarball sha256 `9eef7781095bff104976f04b23dfb84ca4f517b4b13b9c598c0d9516dc631d82` |
| rippled tag history | `features.macro` read at tags 2.5.0, 2.6.2, 3.0.0, 3.1.3, 3.2.1, 3.3.0, 3.3.0-rc1, 3.4.0-b1/b2/b3, 3.4.0-rc1, 3.4.0, 3.4.1, and `develop` (2026-10-10) |
| Official XRPL docs | `XRPLF/xrpl-dev-portal` commit `b591e31668c5c9cb20e98bb83a79f65a9c126131` (2026-10-09): `docs/concepts/tokens/single-asset-vaults.md`, `docs/concepts/tokens/lending-protocol.md`, `docs/references/protocol/transactions/types/{loanset,loanpay,vaultcreate}.md` |

Source paths below are relative to the rippled 3.4.1 tree. The lending and vault
transactors are byte-identical between 3.4.0 and 3.4.1 (`diff -rq` over
`src/libxrpl/tx/transactors/{lending,vault}` is empty). The 3.4.1 delta that touches
this area is the added `fixBatchV1_2` amendment and overflow guards in
`TokenHelpers.cpp`.

The Credence build plan was not used as evidence of XRPL behaviour anywhere in this file.

---

## Q1. First rippled release containing every required feature, including LendingProtocolV1_1

**Expected.** `LendingProtocolV1_1` first appears in **3.3.0** (release 2026-08-06), declared
`Supported::No`. It first becomes `Supported::Yes` in **3.4.0** (release 2026-09-17; the flip is
visible from `3.4.0-b3`). `fixCleanup3_4_0` first appears in 3.4.0, and it carries the
role-separated counterparty signing prefixes (see Q7). **3.4.0 is the first release in which every
feature Credence needs is present and supported.** 3.4.1 (2026-10-09) is the current release and
leaves the lending and vault transactors unchanged.

| Tag | SingleAssetVault | LendingProtocol | LendingProtocolV1_1 | fixCleanup3_4_0 |
| --- | --- | --- | --- | --- |
| 3.0.0 | No | No | absent | absent |
| 3.1.3 | Yes | Yes | absent | absent |
| 3.2.1 | Yes | Yes | absent | absent |
| 3.3.0 | Yes | Yes | **No** | absent |
| 3.4.0 / 3.4.1 | Yes | Yes | **Yes** | Yes |

All four are `VoteBehavior::DefaultNo`.

**Basis.** `include/xrpl/protocol/detail/features.macro` at each tag; GitHub releases API.
**Label.** CONFIRMED (source and release metadata).
**Uncertainty.** A 3.3.x build can still switch V1_1 on through `[features]` presets (Q2),
because `getRegisteredFeature` accepts unsupported features. A spike on 3.3.x would be running an
amendment its own release marks unsupported. The docker image tag/digest that maps to the 3.4.x
release is not checked here.
**Experiment.** Record `server_info.build_version`, the image digest, and the `feature` RPC output,
and compare them against the tag above.

## Q2. Amendments that must be enabled

**Expected.**
- Gating (transactions return `temDISABLED` / fail `checkExtraFeatures` without them):
  `SingleAssetVault`, `MPTokensV1` (vault shares are an MPT issuance), `LendingProtocol`,
  `LendingProtocolV1_1` (closed-ended vaults and the phase rules). `LoanSet`, `LoanPay` and the
  broker transactions call `checkLendingProtocolDependencies`
  (`src/libxrpl/ledger/helpers/LendingHelpers.cpp:70-82`). `VaultCreate` rejects
  `VaultKind`/`SubscriptionDate`/`RedemptionDate` unless V1_1 is on
  (`src/libxrpl/tx/transactors/vault/VaultCreate.cpp:47-50`).
- Behaviour-changing, needed for the properties Credence claims: `fixCleanup3_4_0`
  (role-separated counterparty signatures, Q7; exclusive late/default boundaries), and
  `fixCleanup3_1_3`, `fixCleanup3_2_0`, `fixCleanup3_3_0` (overpayment permission result, rounding,
  freeze checks).
- Not needed: `PermissionedDomains` (only if `DomainID` is used), `Batch*`, `Sponsor`.

**How they get enabled in standalone mode (three mechanisms that behave differently).**
1. `--start` (fresh genesis) writes `getDesired()` into the genesis `Amendments` object.
   `getDesired()` is "supported **and** voted Up" (`src/xrpld/app/main/Application.cpp:1684-1695`,
   `src/xrpld/app/misc/detail/AmendmentTable.cpp:779-810`). Every amendment above is `DefaultNo`,
   so **a bare `--start` enables none of them**; each one needs an Up vote through the
   `[amendments]` config section.
2. The `[amendments]` section is **ignored when `wallet.db` already holds votes**
   (`AmendmentTable.cpp:563-575`). A persisted data directory can silently override the config.
3. `[features]` entries become **Rules presets** (`src/xrpld/core/detail/Config.cpp:1163-1176`,
   `src/libxrpl/ledger/ReadView.cpp:67-80`). A preset is treated as enabled by every transactor,
   but it is **never written to the ledger's `Amendments` object**.
- Without `--start`, the standalone server still builds a genesis ledger, but with an empty
  initial amendment list (`Application.cpp:1302-1305`, `1686-1688`). In that case only
  `[features]` presets apply. The current `infra/rippled/docker-compose.yml` runs `--standalone`
  without `--start`.

**Label.** CONFIRMED (source).
**Uncertainty.** Whether the `feature` RPC reports preset features as enabled is not read here.
**Experiment.** Record both `ledger_entry {"amendments": ...}` (the on-ledger list) and the
config's `[features]`/`[amendments]` stanzas. Show that a V1_1-only behaviour works (a
`VaultCreate` with `VaultKind=1` succeeds, and `LoanBrokerSet` on an open-ended vault fails
`tecNO_PERMISSION`). Start from an empty data directory.

## Q3. Closed-ended vault phases and allowed actions

**Expected.** A closed-ended vault is `VaultCreate` with `VaultKind = 1` plus `SubscriptionDate`
and `RedemptionDate` (UINT32 seconds since the XRPL epoch, 2000-01-01). The kind is a field, not a
flag, and absent means open-ended (`VaultCreate.cpp:108-122`). Constraints:
`180 s <= RedemptionDate - SubscriptionDate < 30 years` (`include/xrpl/protocol/Protocol.h:368-370`).
Both dates must still be in the future at creation, else `tecEXPIRED` (`VaultCreate.cpp:167-169`).
The dates are immutable (InvariantCheck).

Phase (`src/libxrpl/ledger/helpers/VaultHelpers.cpp:290-306`; `now` = parent ledger close time):
Subscription while `now <= SubscriptionDate`; Investment while `SubscriptionDate < now < RedemptionDate`;
Redemption once `now >= RedemptionDate`.

| Transaction | Subscription | Investment | Redemption | Source |
| --- | --- | --- | --- | --- |
| VaultDeposit | yes | `tecEXPIRED` | `tecEXPIRED` | `VaultDeposit.cpp:110-118` |
| VaultWithdraw | yes | `tecTOO_SOON` | yes | `VaultWithdraw.cpp:90-97` |
| LoanBrokerSet (create) | yes, closed-ended only | yes | yes | `LoanBrokerSet.cpp:149-161` (open-ended returns `tecNO_PERMISSION`) |
| LoanSet | `tecTOO_SOON` | yes, if final payment + 60 s <= RedemptionDate | `tecEXPIRED` | `LoanSet.cpp:319-344` |
| LoanPay / LoanManage / LoanDelete | no phase check | no phase check | no phase check | `LoanPay.cpp` (no `getVaultPhase` call) |

The docs table (`single-asset-vaults.md:44-53`) agrees. Phases are computed on read; no
transaction is needed to move between them.

**Label.** CONFIRMED (source and docs).
**Uncertainty.** None on the rules. Timing is covered in Q4.
**Experiment.** For each phase, submit deposit, withdraw and LoanSet, and record the engine
result **and** the vault entry (`AssetsTotal`, `AssetsAvailable`, share `OutstandingAmount`)
before and after.

## Q4. How phase timestamps behave in standalone mode

**Expected.**
- The clock is the **parent (last closed) ledger close time**, not wall-clock time
  (`src/libxrpl/ledger/View.cpp:49-63`). A boundary that has passed in wall-clock time has no
  effect until a ledger with a later close time has closed.
- Standalone `ledger_accept` takes no time argument (`LedgerAccept.cpp`). It simulates consensus
  at the time keeper's wall clock (`NetworkOPs.cpp:4521-4536`). The close time is
  `max(round(wall clock, resolution), parent close + 1 s)` (`include/xrpl/ledger/LedgerTiming.h:145-157`,
  `RCLConsensus.cpp:472-487`). Resolution is one of 10/20/30/60/90/120 s (default 30 s, genesis 10 s).
  **Each close advances ledger time by at least 1 s.** A script that closes ledgers faster than
  the resolution drives ledger time *ahead* of wall clock, and the result is not repeatable run to run.
- `LoanSet` sets `StartDate = view.header().closeTime` (`LoanSet.cpp:225-229`, `650`). In the open
  ledger (the provisional result returned by `submit`), that header is copied from the parent
  (`OpenView.cpp:90-104`), so StartDate equals the parent close time. In the ledger actually
  built, the header is the new ledger's close time (`BuildLedger.cpp:47,58`). **The final
  `StartDate` and `NextPaymentDueDate` are later than the provisional ones**, and the 60 s
  redemption-buffer check can pass provisionally but fail at close.
- Payment lateness (`LendingHelpers.cpp:173-180`) and default eligibility (`LoanManage.cpp`
  preclaim) use the same parent-close clock.

**Label.** CONFIRMED (source).
**Uncertainty.** How close-time resolution evolves under `ledger_accept`-only operation. Whether
the docker time keeper is skewed from the host.
**Experiment.** Log `ledger_closed` close_time and host wall time for every close. Show the phase
flip happening on the first ledger whose *parent* close time crosses the boundary. Read
`Loan.StartDate` from the validated ledger and compare it with the provisional value.

## Q5. Objects and transactions for the Loan Broker and first-loss cover

**Expected.**
- `LoanBrokerSet` (no `LoanBrokerID`) creates an `ltLOAN_BROKER` (`ledger_entries.macro:522-540`)
  plus a broker pseudo-account. Only the **vault owner** may submit it (`LoanBrokerSet.cpp:109-113`).
  The fixed parameters are `ManagementFeeRate`, `CoverRateMinimum` and `CoverRateLiquidation`
  (both cover rates zero or both non-zero; `LoanBrokerSet.cpp:72-80`). `DebtMaximum` is mutable.
- `LoanBrokerCoverDeposit` (broker owner only) moves the vault asset to the broker pseudo-account
  and raises `LoanBroker.CoverAvailable`. `LoanBrokerCoverWithdraw` cannot take cover below
  `DebtTotal x CoverRateMinimum`.
- At `LoanSet`, `CoverAvailable >= (DebtTotal + new debt) x CoverRateMinimum` is required, else
  `tecINSUFFICIENT_FUNDS` (`LoanSet.cpp:559-578`).
- On default, cover pays
  `min(DebtTotal x CoverRateMinimum x CoverRateLiquidation, DefaultAmount, CoverAvailable)` and the
  vault absorbs the remainder (`LoanManage.cpp:135-172`). **First-loss cover is bounded by its
  rates.** With `CoverRateMinimum = 0` it covers nothing.

**Label.** CONFIRMED (source).
**Uncertainty.** None on the mechanics.
**Experiment.** Record the `LoanBroker` entry after each step (`CoverAvailable`, `DebtTotal`,
`CoverRateMinimum`, `CoverRateLiquidation`). If a default is exercised, record cover and vault
deltas.

## Q6. Signatures and authorizations LoanSet requires

**Expected.**
1. **The `Account` signature**, through the normal `Transactor::checkSign`: single key (master if
   not disabled, or the regular key) or a `Signers` multisig meeting the account's `SignerList`
   quorum (`LoanSet.cpp:156-158`; `Transactor.cpp:918-1206`).
2. **A `CounterpartySignature` object**, required outside Batch (`temBAD_SIGNER`;
   `LoanSet.cpp:85-94`). It holds *either* `SigningPubKey` + `TxnSignature` *or* `Signers`. It is
   verified against the **counterparty account's** own keys and signer list by the same
   `checkSign` machinery (`LoanSet.cpp:164-180`).
3. **Role binding.** Either `Account` or `Counterparty` must be the `LoanBroker.Owner`, else
   `tecNO_PERMISSION` (`LoanSet.cpp:293-300`). `Counterparty` defaults to the broker owner.
   Borrower = `Account` if `Counterparty` is the broker owner, else `Counterparty`
   (`LoanSet.cpp:303`, `453`).
4. **Fee.** `base x (1 + number of Account signers) + base x (number of counterparty signers, or 1
   for a single signature)` (`LoanSet.cpp:184-208`). For a 2-of-2 borrower `Account` and a
   single-signed broker counterparty that is **4 x base**.
5. **What both signatures cover.** Every field except `TxnSignature`, `Signers`,
   `CounterpartySignature`, `SponsorSignature` and `BatchSigners` (`sfields.macro:280,411,412,418,444`).
   That includes `Account`, `Counterparty`, `LoanBrokerID`, every term, `Flags`, `Fee`,
   `Sequence`, `LastLedgerSequence`, and the top-level `SigningPubKey` (empty for a multisigned
   `Account`). **All of these must be fixed before either party signs.**

**Label.** CONFIRMED (source and `loanset.md`).
**Experiment.** See Q8 to Q10.

## Q7. Is the Loan Broker counterparty signature a separate authorization domain?

**Expected.** Yes, in two layers, and the second depends on an amendment.
- **Separate authority:** it is checked against the counterparty account's keys and signer
  list, independently of `Account` (`LoanSet.cpp:179-180`). The account owner's "may not
  multisign for itself" rule is not applied to the counterparty object (`STTx.cpp:582-583`).
- **Separate signing domain only with `fixCleanup3_4_0`:** counterparty signatures use hash
  prefix `CPT` (single) / `CPM` (multi) instead of the transaction prefixes
  (`src/libxrpl/protocol/Sign.cpp:54-72`, `include/xrpl/protocol/HashPrefix.h:99,104`).
  **Without `fixCleanup3_4_0`, every role signs identical bytes** ("a signature could be moved
  from one role to another", `Sign.cpp:56-58`). The upstream test
  `src/test/app/lending/LoanLifecycle_test.cpp:200-215` shows a copied account signature
  rejected in the counterparty slot.

**Label.** CONFIRMED (source and upstream test).
**Uncertainty.** None about the code. It matters whether the spike enabled `fixCleanup3_4_0`.
**Experiment.** Copy the borrower-side signature (or one signer's signature) into
`CounterpartySignature` and confirm rejection with `fixCleanup3_4_0` on. Record the
amendment state.

## Q8. Can the borrower satisfy its side of LoanSet through a 2-of-2 signer list?

**Expected.** Yes, in either role:
(a) borrower as `Account`, multisigned in top-level `Signers`, with `Counterparty` omitted
(it defaults to the broker owner); or (b) broker owner as `Account` with
`Counterparty = borrower`, and the borrower's 2-of-2 `Signers` inside `CounterpartySignature`.
Path (b) is exercised positively upstream (`LoanSet_test.cpp:493-547`, "lender submits, borrower
multisign"). For path (a), upstream only exercises the negative cases (`LoanTestBase.h:1964-1999`:
bad signers give `tefBAD_SIGNATURE`, no signer list gives `tefNOT_MULTI_SIGNING`, low fee gives
`telINSUF_FEE_P`).

**Label.** LIKELY for (a); CONFIRMED by an upstream test for (b), though not on Credence's account
shape (master key on a separate device, quorum 2, weights 1/1).
**Uncertainty.** No upstream positive test of (a). Upstream fixtures leave the borrower's master
key enabled, so a false positive from master-key signing has to be excluded.
**Experiment.** Borrower with `SignerList` {agent, policy} quorum 2 and master key held elsewhere.
Submit (a) with `SigningPubKey = ""` and two `Signers`. Then confirm: the tx JSON has no top-level
`TxnSignature`; `Loan.Borrower` equals the 2-of-2 account; the principal arrived in that account's
trust line. Negative controls: one signer only gives `tefBAD_QUORUM`; an agent-only single
signature gives `tefBAD_AUTH`.

## Q9. Can the borrower multisignature coexist with the broker counterparty authorization?

**Expected.** Yes. Top-level `Signers` and `CounterpartySignature` are separate non-signing
fields, verified separately (`STTx.cpp:271-281`; `LoanSet.cpp:156-181`). The upstream negative
tests (`LoanTestBase.h:1966-1985`) build exactly this shape (`Msig(...)` plus
`Msig(sfCounterpartySignature, ...)`).

**Label.** LIKELY (path and negative tests; no upstream positive test on this exact shape).
**Experiment.** Same tx as Q8(a), plus a single-signed `CounterpartySignature` from the broker
owner. The fee must be 4 x base or more. Then confirm the `Loan`, the `LoanBroker.DebtTotal` delta,
the vault `AssetsAvailable` delta and the borrower balance delta in the validated ledger.

## Q10. What happens if the counterparty signature was made over different terms?

**Expected.** Any change to a signed field after the counterparty signed (a term, `Fee`,
`Sequence`, `LastLedgerSequence`, `Counterparty`, `Flags`, `Account`, or the top-level
`SigningPubKey`) invalidates the signature. `STTx::checkSign` fails with "Counterparty: Invalid
signature" and the transaction is rejected as malformed before it reaches a ledger: no fee, no
sequence consumed, no hash in any ledger (`STTx.cpp:271-281`; test pattern `LoanLifecycle_test.cpp:186-197`).
Corollary: a broker signature cannot be reused across loans, because the borrower's `Sequence`
is covered.

**Label.** CONFIRMED (source).
**Experiment.** Have the broker sign terms T. Submit T' (for example, a larger `PrincipalRequested`)
with the same counterparty signature. Expect rejection at submit (`invalidTransaction` /
`fails local checks`), no Loan, and broker `DebtTotal` unchanged.

## Q11. Can a 2-of-2 borrower execute LoanPay?

**Expected.** Yes. `LoanPay` needs `Account == Loan.Borrower` (`LoanPay.cpp:244-248`) and uses the
normal `checkSign`, so a quorum multisig qualifies. Fee is `base x (1 + signers) x feeIncrements`.
`feeIncrements` = 1 for full/late payments or when 5 or fewer payments remain, otherwise grows with
the number of periodic payments the amount covers (`LoanPay.cpp:112-226`).

**Label.** LIKELY (no upstream multisigned LoanPay test found).
**Experiment.** Multisigned LoanPay. Check that the tx has no `TxnSignature`, that `Loan.PaymentRemaining`
drops and `PreviousPaymentDueDate`/`NextPaymentDueDate` advance, and that the borrower balance
drops by exactly the amount charged (not necessarily `Amount`).

## Q12. Scheduled, early, partial and overpayment behaviour, and the flags involved

**Expected** (`LendingHelpers.cpp:2253-2310`, `1046-1250`, `1662-1800`; `loanpay.md`; `lending-protocol.md:185-205`).
- **Regular (no flag):** accepted any time before the due date, including early. Pays as many
  **whole** periodic installments (`PeriodicPayment` rounded up + `LoanServiceFee`) as `Amount`
  covers, up to 100. **Anything below one installment fails with `tecINSUFFICIENT_PAYMENT`.**
  There is no sub-installment partial payment. Any excess over whole installments is not taken.
  Paying early does not reduce scheduled interest.
- **Overpayment (`tfLoanOverpayment` 0x00010000):** needs the Loan to carry `lsfLoanOverpayment`,
  which is set only by `tfLoanOverpayment` on **LoanSet**. Without it the result is
  `tecNO_PERMISSION` under `fixCleanup3_1_3` (`LoanPay.cpp:250-254`). The excess after whole
  installments reduces principal, minus `OverpaymentFee` / `OverpaymentInterestRate`.
- **Full early (`tfLoanFullPayment` 0x00020000):** no loan-level flag is needed. It is rejected
  with `tecKILLED` when 1 or fewer payments remain. It must cover principal, accrued interest,
  `CloseInterestRate` on principal, `ClosePaymentFee` and outstanding fees, else
  `tecINSUFFICIENT_PAYMENT`.
- **Late (`tfLoanLatePayment` 0x00040000):** required once `now > NextPaymentDueDate`. An
  unflagged payment then fails `tecEXPIRED`; the flag used too early gives `tecTOO_SOON`. It adds
  `LatePaymentFee` plus late interest. Overpayment is not allowed on a late payment.
- At most one flag per tx (`temINVALID_FLAG`). Repayments have no vault-phase restriction.

**Label.** CONFIRMED (source and docs).
**Experiment.** On a loan *without* `lsfLoanOverpayment`: an amount one micro-unit below an
installment gives `tecINSUFFICIENT_PAYMENT`; exactly one installment early gives tesSUCCESS with
due dates advanced; 1.5 installments charges only one; the overpayment flag gives `tecNO_PERMISSION`;
full payment gives tesSUCCESS with the Loan zeroed. Record the borrower delta against `Amount`
each time.

## Q13. Ledger fields that expose loan state

**Expected** (`ltLOAN`, `ledger_entries.macro:546-620`).
- Outstanding principal: `PrincipalOutstanding`.
- Total owed: `TotalValueOutstanding`. Interest outstanding is *derived*:
  `TotalValueOutstanding - PrincipalOutstanding`, of which `ManagementFeeOutstanding` goes to
  the broker. **No field holds accrued or paid-to-date interest.** That has to come from LoanPay
  metadata (balance deltas) or be recomputed.
- Fees: `LoanOriginationFee`, `LoanServiceFee`, `LatePaymentFee`, `ClosePaymentFee`,
  `OverpaymentFee`, and the rates `InterestRate`, `LateInterestRate`, `CloseInterestRate`,
  `OverpaymentInterestRate` (1/10 bp).
- Schedule: `StartDate`, `PaymentInterval`, `GracePeriod`, `PreviousPaymentDueDate`,
  `NextPaymentDueDate`, `PaymentRemaining`, `PeriodicPayment` (unrounded, rounded up when paid),
  `LoanScale`. **No field holds "next amount due"**; it is `roundUp(PeriodicPayment, LoanScale) +
  LoanServiceFee`, plus late components when late.
- Impairment/default: flags `lsfLoanImpaired` 0x00020000 and `lsfLoanDefault` 0x00010000
  (`LedgerFormats.h:205-207`). Vault `LossUnrealized` while impaired.
- **Units:** for an IOU asset, `LoanScale` is the exponent of the total loan value
  (`LendingHelpers.cpp:2181-2198`). A loan of about 500 has a scale near 1e-13. Ledger amounts are
  therefore **not** integer micro-units.

**Label.** CONFIRMED (source).
**Experiment.** Dump the full Loan entry after LoanSet and after each LoanPay. Reconcile
`TotalValueOutstanding` deltas with the borrower, vault pseudo-account and broker balance deltas
in metadata.

## Q14. Does the owner's master key keep its escape-hatch authority?

**Expected.** Yes, provided `lsfDisableMaster` is never set.
- An enabled master key can sign any tx alone regardless of the signer list
  (`Transactor.cpp:1019-1048`).
- Disabling the master key **requires a master-key signature**; a multisig quorum gets
  `tecNEED_MASTER_KEY` (`AccountSet.cpp:307-313`).
- The 2-of-2 quorum *can* `SignerListSet` (replace or delete the list) and `SetRegularKey`.
  It cannot remove the master key, and the master key can undo either change.
- Deleting the signer list is permitted while the master key is enabled (`SignerListSet.cpp:357-366`).
- Corollary: while the list exists, **any** quorum-signed LoanSet or Payment is valid on-ledger.
  The ledger does not require the master key for borrowing.

**Label.** CONFIRMED (source).
**Uncertainty.** Out of scope here: whether the Ledger device XRP app can sign these types (a
separate spike).
**Experiment.** With the signer list in place: a master-key Payment gives tesSUCCESS; a
multisigned `AccountSet SetFlag=4` gives `tecNEED_MASTER_KEY` and the flag stays clear; a master
`SignerListSet` with quorum 0 deletes the list, after which a multisigned spend gives
`tefNOT_MULTI_SIGNING`.

## Q15. Plan assumptions the protocol already contradicts or conditions (before the spike)

This is a pre-spike list. `PLAN-DIFF.md` has the full classification.
1. **CONTRADICTED:** "make an early partial payment" (Repayment, spike sentence). There is no
   sub-installment payment (Q12). "Early" means whole installments ahead of schedule, a full
   early payment, or an overpayment (the last needs the loan flag set at origination).
2. **CONTRADICTED (as stated):** "an early or full payment only if the loan's flags allow it."
   Early whole installments and full payment need no loan flag. Only overpayment does.
3. **CONTRADICTED (unconditional form):** "Loss absorbed by Credence's first-loss cover before
   lenders lose anything." Cover is capped by `DebtTotal x CoverRateMinimum x CoverRateLiquidation`
   and by `CoverAvailable` (Q5).
4. **Plan ambiguity, reported and not resolved:** Transaction flow step 4 (preferred: the
   Ledger device "signs the borrower side of the loan directly") against the exit criterion
   "LoanSet with a 2-of-2 borrower." One signature slot cannot hold both a master single-signature
   and a 2-of-2 multisig ("Cannot both single- and multi-sign", `STTx.cpp:493-494`). These are two
   different LoanSet authorizations.
5. **Conditioned:** "regular key disabled." No such ledger state exists; it is just the absence of
   `RegularKey`, and the 2-of-2 quorum can add one (Q14).
6. **Conditioned:** "ResearchBot cannot take on new debt without owner approval." The ledger
   accepts a quorum-signed LoanSet without the owner, so this rests on the Loan Broker signer's
   off-ledger refusal. (The plan's attack table already attributes it to the Loan Broker signer.)
7. **Conditioned:** revocation by "SignerListSet removing ResearchBot." If the replacement list
   leaves the policy key able to reach quorum alone, Credence could spend alone. The list has to
   be deleted (quorum 0) or ResearchBot replaced.
8. **Conditioned:** integer micro-units. An IOU tRLUSD's ledger loan amounts sit at `LoanScale`
   (about 1e-13). The repayment scheduler has to map ledger amounts to micro-units and round up
   to pay an installment. An MPT-based tRLUSD with AssetScale 6 would make ledger units equal
   micro-units. Not tested here.
9. **CONFIRMED:** closed-ended restriction, phase table, fixed dates, investment-only
   origination, final payment inside the investment phase (60 s buffer), master key cannot be
   disabled by the signer list.
