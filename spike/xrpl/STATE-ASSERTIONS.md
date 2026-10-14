# XRPL E3/E4 state-assertion specification

This is the predeclared oracle for a separately authorized E3/E4 rerun. It was
written during Checkpoint 1C-H using only committed evidence and pinned rippled
3.4.0 source. Checkpoint 1C-H submitted zero transactions and made no new
protocol observation.

## Common outcome rules

- Positive PASS requires the expected `tesSUCCESS`, a validated lookup with
  matching `meta.TransactionResult`, and every required semantic predicate.
- Expected-negative PASS requires the exact authoritative rejection (or an
  explicitly predeclared client exception) and equality of every protected
  state field. An absent or unvalidated lookup cannot erase the submission
  result.
- Ledger-entry arrays are unordered. Objects are identified by
  `LedgerEntryType`, ledger index or protocol ID when available, and their
  stable relationship fields. Total object count is never an oracle.
- A changed ledger header, close time, or ledger index is not treated as a
  mutation of protected protocol state.
- Zero-valued default fields may be omitted by JSON serialization where pinned
  source declares them defaulted; the oracle normalizes those specific fields
  to zero and no others.
- `FAIL` means an authoritative result or observable semantic predicate differs
  from this specification. `BLOCKED` means required evidence cannot be obtained
  or uniquely identified, the response is malformed, or the harness cannot
  make the predeclared comparison.

## A. E3 VaultCreate

- **PRE-STATE OBJECTS USED:** owner `AccountRoot`; owner tRLUSD `RippleState`;
  owner objects searched for the future Vault ID and share issuance relationship.
- **EXPECTED SUBMISSION RESULT:** `tesSUCCESS`.
- **REQUIRED VALIDATION STATUS:** `VALIDATED`, with validated
  `meta.TransactionResult = tesSUCCESS` and exactly one created `Vault` index in
  metadata.
- **POST-STATE OBJECTS USED:** `Vault` at the created index; owner `MPToken`
  whose `MPTokenIssuanceID` equals the Vault `ShareMPTID`; share
  `MPTokenIssuance`; Vault pseudo-account `AccountRoot` where metadata exposes it.
- **REQUIRED SEMANTIC PREDICATES:** Vault `Owner`, `Asset`, `VaultKind = 1`,
  `SubscriptionDate`, `RedemptionDate`, `WithdrawalPolicy`, flags, and index
  match the submitted terms; the owner MPToken has the submitted owner as
  `Account` and the Vault share issuance as `MPTokenIssuanceID`; neither
  identified object existed in pre-state.
- **PROTECTED STATE THAT MUST REMAIN UNCHANGED:** the pre-existing owner tRLUSD
  trust-line identity and balance; submitted asset identity and dates.
- **AUXILIARY OBJECTS THAT MAY LEGITIMATELY APPEAR:** `MPTokenIssuance`, Vault
  pseudo-account `AccountRoot`, directory nodes, the owner `MPToken`, and the
  pseudo-account asset holding/`RippleState` shown by pinned source and committed
  evidence.
- **WHAT WOULD CAUSE FAIL:** missing/wrong Vault; missing/wrong owner MPToken;
  wrong owner, asset, kind, dates, policy, flags, share relationship, or
  validated result.
- **WHAT WOULD CAUSE BLOCKED:** no unique created Vault ID in validated metadata,
  unavailable post-state, or an unrecognized representation that prevents the
  specified identity checks.

## B. E3 Subscription deposit

- **PRE-STATE OBJECTS USED:** Vault by ID; share issuance by `ShareMPTID`;
  lender MPToken by lender account plus issuance ID (possibly absent before the
  first deposit); lender tRLUSD `RippleState`; validated close time.
- **EXPECTED SUBMISSION RESULT:** `tesSUCCESS`.
- **REQUIRED VALIDATION STATUS:** `VALIDATED` with matching validated result.
- **POST-STATE OBJECTS USED:** the same Vault, issuance, lender MPToken, and
  lender trust line.
- **REQUIRED SEMANTIC PREDICATES:** close time is in Subscription
  (`now <= SubscriptionDate`); Vault ID/owner/asset/share ID remain stable;
  `AssetsTotal` and `AssetsAvailable` increase by the deposited tRLUSD amount;
  lender tRLUSD balance decreases by that amount; issuance
  `OutstandingAmount` and lender `MPTAmount` increase by the exact share amount
  derived with the Vault share scale.
- **PROTECTED STATE THAT MUST REMAIN UNCHANGED:** Vault owner, asset, kind,
  dates, policy, share issuance ID, and unrelated actors' balances.
- **AUXILIARY OBJECTS THAT MAY LEGITIMATELY APPEAR:** the lender MPToken and its
  directory linkage on first deposit; modified trust-line and issuance nodes.
- **WHAT WOULD CAUSE FAIL:** wrong result/validation, phase, asset movement,
  share movement, holder identity, or stable Vault relationship.
- **WHAT WOULD CAUSE BLOCKED:** share scale or issuance relationship cannot be
  read, or any required pre/post object is unavailable.

## C. E3 Subscription withdrawal

- **PRE-STATE OBJECTS USED:** Vault, issuance, lender MPToken, lender tRLUSD
  trust line, and validated close time, all by the identities in B.
- **EXPECTED SUBMISSION RESULT:** `tesSUCCESS`.
- **REQUIRED VALIDATION STATUS:** `VALIDATED` with matching validated result.
- **POST-STATE OBJECTS USED:** the same four protocol objects.
- **REQUIRED SEMANTIC PREDICATES:** Subscription phase; Vault asset totals and
  available assets decrease by the withdrawal amount; lender tRLUSD increases
  by that amount; outstanding and held shares decrease by the exact scaled
  share amount; identities and relationships remain stable.
- **PROTECTED STATE THAT MUST REMAIN UNCHANGED:** Vault configuration and all
  unrelated balances/objects.
- **AUXILIARY OBJECTS THAT MAY LEGITIMATELY APPEAR:** modified issuance,
  MPToken, trust-line, and directory nodes; no global object-count restriction.
- **WHAT WOULD CAUSE FAIL:** any wrong delta, identity, phase, result, or
  validation status.
- **WHAT WOULD CAUSE BLOCKED:** required object or exact scale/delta cannot be
  determined.

## D. E3 Investment deposit rejection

- **PRE-STATE OBJECTS USED:** Vault, issuance, lender MPToken, lender tRLUSD
  trust line, and close time.
- **EXPECTED SUBMISSION RESULT:** `tecEXPIRED`.
- **REQUIRED VALIDATION STATUS:** submission result is authoritative;
  `NOT_IN_VALIDATED_LEDGER` is acceptable and must not overwrite it.
- **POST-STATE OBJECTS USED:** the same protected objects after a validating
  close or absence check.
- **REQUIRED SEMANTIC PREDICATES:** close time is strictly after
  `SubscriptionDate` and before `RedemptionDate`; every protected field is
  unchanged.
- **PROTECTED STATE THAT MUST REMAIN UNCHANGED:** Vault totals/configuration,
  issuance outstanding amount, lender MPTAmount, lender tRLUSD balance, and the
  relevant account sequence when the transaction was not included.
- **AUXILIARY OBJECTS THAT MAY LEGITIMATELY APPEAR:** none attributable to the
  rejected transaction; ledger header progression is ignored.
- **WHAT WOULD CAUSE FAIL:** result other than `tecEXPIRED` or any protected
  mutation.
- **WHAT WOULD CAUSE BLOCKED:** authoritative submission response or protected
  pre/post state cannot be obtained.

## E. E3 Investment withdrawal rejection

- **PRE-STATE OBJECTS USED:** the same objects and identities as D.
- **EXPECTED SUBMISSION RESULT:** `tecTOO_SOON`.
- **REQUIRED VALIDATION STATUS:** submission result is authoritative;
  non-inclusion must not erase it.
- **POST-STATE OBJECTS USED:** the same protected objects.
- **REQUIRED SEMANTIC PREDICATES:** Investment phase and exact protected-state
  equality.
- **PROTECTED STATE THAT MUST REMAIN UNCHANGED:** the complete protected set in D.
- **AUXILIARY OBJECTS THAT MAY LEGITIMATELY APPEAR:** none attributable to the
  rejected transaction; ledger header progression is ignored.
- **WHAT WOULD CAUSE FAIL:** wrong rejection or any protected mutation.
- **WHAT WOULD CAUSE BLOCKED:** missing authoritative result or incomparable
  protected state.

## F. E3 Redemption withdrawal

- **PRE-STATE OBJECTS USED:** Vault, issuance, lender MPToken, lender tRLUSD
  trust line, and close time.
- **EXPECTED SUBMISSION RESULT:** `tesSUCCESS`.
- **REQUIRED VALIDATION STATUS:** `VALIDATED` with matching validated result.
- **POST-STATE OBJECTS USED:** the same four protocol objects.
- **REQUIRED SEMANTIC PREDICATES:** `now >= RedemptionDate`; the Vault totals,
  lender balance, outstanding shares, and lender shares move by the exact
  withdrawal/scaled-share deltas specified in C; stable identities remain equal.
- **PROTECTED STATE THAT MUST REMAIN UNCHANGED:** Vault configuration and
  unrelated actor state.
- **AUXILIARY OBJECTS THAT MAY LEGITIMATELY APPEAR:** modified known holdings,
  issuance, and directories; no global count restriction.
- **WHAT WOULD CAUSE FAIL:** wrong phase, result, validation, identity, or delta.
- **WHAT WOULD CAUSE BLOCKED:** a required object or exact delta cannot be read.

## G. E3 Redemption deposit rejection

- **PRE-STATE OBJECTS USED:** the protected set from D and close time.
- **EXPECTED SUBMISSION RESULT:** `tecEXPIRED`.
- **REQUIRED VALIDATION STATUS:** authoritative submission rejection;
  non-inclusion is acceptable and cannot erase it.
- **POST-STATE OBJECTS USED:** the same protected set.
- **REQUIRED SEMANTIC PREDICATES:** `now >= RedemptionDate` and exact protected
  state equality.
- **PROTECTED STATE THAT MUST REMAIN UNCHANGED:** Vault totals/configuration,
  issuance, lender shares/balance, and relevant non-included account sequence.
- **AUXILIARY OBJECTS THAT MAY LEGITIMATELY APPEAR:** none attributable to the
  rejected transaction; ledger header progression is ignored.
- **WHAT WOULD CAUSE FAIL:** wrong result or protected mutation.
- **WHAT WOULD CAUSE BLOCKED:** required result/state is unavailable.

## H. E4 Loan Broker creation

- **PRE-STATE OBJECTS USED:** closed-ended Vault by ID; Vault owner
  `AccountRoot`; owner tRLUSD `RippleState`; owner objects searched for the
  future LoanBroker identity.
- **EXPECTED SUBMISSION RESULT:** `tesSUCCESS` from LoanBrokerSet submitted by
  the Vault owner.
- **REQUIRED VALIDATION STATUS:** `VALIDATED`, with matching result and exactly
  one created `LoanBroker` index in metadata.
- **POST-STATE OBJECTS USED:** LoanBroker at that index, its pseudo-account,
  referenced Vault, owner AccountRoot, and broker asset holding.
- **REQUIRED SEMANTIC PREDICATES:** `LedgerEntryType = LoanBroker`; index equals
  metadata; `Owner` equals Vault owner; `VaultID` equals the referenced Vault;
  `Account` identifies the broker pseudo-account; `DebtMaximum`,
  `CoverRateMinimum`, `CoverRateLiquidation`, and management fee equal submitted
  values; defaulted `DebtTotal` and `CoverAvailable` normalize to zero; source-
  required owner reserve/owner-count effects are consistent without asserting a
  global object count.
- **PROTECTED STATE THAT MUST REMAIN UNCHANGED:** Vault identity/configuration
  and owner tRLUSD balance.
- **AUXILIARY OBJECTS THAT MAY LEGITIMATELY APPEAR:** LoanBroker pseudo-account,
  empty asset holding/`RippleState`, and directory nodes. Pinned source states
  that owner count increases for both broker and pseudo-account.
- **WHAT WOULD CAUSE FAIL:** wrong owner/Vault relationship, limits/rates,
  nonzero unexpected initial debt/cover, result, or validation.
- **WHAT WOULD CAUSE BLOCKED:** no unique created LoanBroker ID or unavailable
  referenced objects.

## I. E4 vault/broker relationship validation

- **PRE-STATE OBJECTS USED:** the Vault from A and the H transaction evidence.
- **EXPECTED SUBMISSION RESULT:** inherited from H (`tesSUCCESS`); this is a
  post-transaction semantic validation, not a separate submission.
- **REQUIRED VALIDATION STATUS:** inherited H `VALIDATED` status.
- **POST-STATE OBJECTS USED:** Vault by ID and LoanBroker by created ID.
- **REQUIRED SEMANTIC PREDICATES:** LoanBroker `VaultID` equals Vault index;
  LoanBroker `Owner` equals Vault `Owner`; Vault and broker pseudo `Account`
  fields are present and distinct protocol identities.
- **PROTECTED STATE THAT MUST REMAIN UNCHANGED:** Vault owner, asset, kind, dates,
  share issuance, and withdrawal policy.
- **AUXILIARY OBJECTS THAT MAY LEGITIMATELY APPEAR:** the H auxiliary objects.
- **WHAT WOULD CAUSE FAIL:** either relationship equality is false or a stable
  Vault identity field changed.
- **WHAT WOULD CAUSE BLOCKED:** either referenced object cannot be fetched by ID.

## J. E4 cover establishment

- **PRE-STATE OBJECTS USED:** LoanBroker by ID, its owner tRLUSD trust line,
  broker pseudo-account asset holding, and referenced Vault.
- **EXPECTED SUBMISSION RESULT:** `tesSUCCESS` for LoanBrokerCoverDeposit by the
  LoanBroker owner.
- **REQUIRED VALIDATION STATUS:** `VALIDATED` with matching result.
- **POST-STATE OBJECTS USED:** the same LoanBroker and holdings/trust lines.
- **REQUIRED SEMANTIC PREDICATES:** LoanBroker ID/Owner/VaultID/rates/limits stay
  stable; `CoverAvailable` increases by the scale-rounded deposit amount; owner
  tRLUSD decreases by that exact amount; broker pseudo-account holding increases
  consistently; `DebtTotal` remains zero before any loan.
- **PROTECTED STATE THAT MUST REMAIN UNCHANGED:** Vault configuration, broker
  limits/rates/relationships, debt total, and unrelated balances.
- **AUXILIARY OBJECTS THAT MAY LEGITIMATELY APPEAR:** modified trust-line,
  LoanBroker, and directory nodes; no global count restriction.
- **WHAT WOULD CAUSE FAIL:** wrong cover/balance delta, identity mutation,
  unexpected debt mutation, result, or validation.
- **WHAT WOULD CAUSE BLOCKED:** scale-rounded amount or required holding cannot
  be observed exactly.

## K. E4 negative broker operation

The predeclared negative is the current planned non-owner LoanBrokerSet against
the E4 Vault; this step does not silently substitute a different cover-negative.

- **PRE-STATE OBJECTS USED:** Vault, owner AccountRoot, non-owner AccountRoot,
  owner/non-owner object sets, and relevant tRLUSD holdings.
- **EXPECTED SUBMISSION RESULT:** `tecNO_PERMISSION`.
- **REQUIRED VALIDATION STATUS:** authoritative submission rejection;
  non-inclusion is acceptable and cannot erase it.
- **POST-STATE OBJECTS USED:** the same protected objects and an explicit lookup
  for absence of a newly created LoanBroker.
- **REQUIRED SEMANTIC PREDICATES:** no LoanBroker for the attempted owner/Vault
  relationship appears; all protected objects/fields remain unchanged.
- **PROTECTED STATE THAT MUST REMAIN UNCHANGED:** Vault, owner and non-owner
  account sequences when not included, object sets by semantic identity, and
  tRLUSD balances.
- **AUXILIARY OBJECTS THAT MAY LEGITIMATELY APPEAR:** none attributable to the
  rejected transaction; ledger header progression is ignored.
- **WHAT WOULD CAUSE FAIL:** wrong rejection, any created matching LoanBroker,
  or protected mutation.
- **WHAT WOULD CAUSE BLOCKED:** result or protected state cannot be compared.

## Static audit disposition

| Hazard | Disposition |
| --- | --- |
| Global owner/ledger object-count oracle | Removed from VaultCreate and LoanBroker creation; matching-object cardinality is scoped by type and identity. |
| List/array position as identity | Prevented by unordered semantic search. |
| `tesSUCCESS`-only PASS | Prevented by validated-result and semantic-predicate requirements. |
| Negative-result overwrite | Prevented by separate submission and lookup fields; committed `tecEXPIRED` fixture is a regression test. |
| Comparison of unrelated auxiliary state | Prevented by explicit protected-state lists above; auxiliary objects do not fail a valid transition. |
| Unstable identity fields | Transaction-history fields, owner-directory positions, array order, and global counts are excluded from identity. |

All object identities required for A-K are derivable from committed evidence and
pinned source. This document does not resolve any protocol result in advance;
the future run must still fail closed on any contradictory observation.
