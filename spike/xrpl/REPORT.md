# Checkpoint 1C-H — XRPL state-assertion harness hardening

## Outcome

**Harness checkpoint: PASS. Protocol experiments remain frozen.**

Checkpoint 1C-H began at clean commit
`546e39523c0b211cde994fa31af6a9eed789220d`, retained checkpoint-0 ancestry
and unchanged submodule gitlinks, and modified only `spike/xrpl/`.

**ZERO new XRPL protocol transactions were submitted.** No ledger was started,
queried, closed, advanced, or mutated. All work used committed evidence, pinned
rippled 3.4.0 source, and synthetic test copies.

## False-block reproduction

The committed Checkpoint 1C-R record proves all parts of the diagnosis:

- submission result: `tesSUCCESS`;
- validated lookup status: `VALIDATED`;
- validated metadata result: `tesSUCCESS`;
- the post-state contains the expected closed-ended tRLUSD `Vault` at index
  `8AF0F702407DF51DA7E5257C3E8DF57886226D751E119AC67EA7B07071995302`;
- the post-state contains an owner `MPToken` whose `Account` is the submitted
  Vault owner and whose `MPTokenIssuanceID` equals the Vault `ShareMPTID`;
- the owner-object list grew from one object to three, a delta of two.

The exact invalid path was the `create_closed_vault` callback in
`scripts/run_checkpoint_1c_r.py`: it asserted `len(after["objects"]) -
len(before["objects"]) == 1`. Pinned `VaultCreate.cpp` explicitly creates and
authorizes an MPToken for the Vault owner. The committed validated metadata also
shows the Vault, owner MPToken, share issuance, pseudo-account, asset holding,
and directory effects. The old total-count expectation was therefore not a
protocol invariant.

H1 reproduces the old false assertion (`expected 1`, `actual 2`, FAIL) and then
classifies the same committed evidence PASS with semantic assertions.

## Semantic assertion model

The harness now:

- searches unordered collections by `LedgerEntryType`, index/protocol ID, and
  explicit identity/relationship fields;
- asserts Vault owner, asset, kind, dates, policy, flags, and share relationship;
- asserts an owner MPToken by holder account plus the Vault `ShareMPTID`;
- permits unrelated auxiliary objects unless pinned semantics forbid them;
- normalizes omission only for protocol fields explicitly defaulted to zero;
- requires validated success plus all semantic predicates for a positive PASS;
- requires the exact negative result plus unchanged protected state for a
  negative PASS; and
- retains submission and lookup results separately so an unvalidated lookup
  cannot erase an authoritative rejection.

No global object-count or array-position assertion remains in the planned
VaultCreate or LoanBroker creation paths.

## Object-specific helpers

`scripts/harness.py` now provides semantic helpers for:

- Vault identity and closed-ended configuration;
- MPToken holder/issuance identity and optional amount/flags;
- RippleState endpoints, asset, and balance;
- AccountRoot account identity and optional flags;
- LoanBroker owner/Vault relationship, debt limits, cover rates, debt, and cover;
- complete VaultCreate and LoanBroker creation transitions; and
- cover establishment deltas on a known LoanBroker and owner balance.

`scripts/run_checkpoint_1c_r.py` now references those helpers for the future
VaultCreate, LoanBroker creation, and cover checks. The script was not executed
during 1C-H.

## H1-H12 results

| Test | Result | Evidence established |
| --- | --- | --- |
| H1 real VaultCreate fixture accepted | PASS | Expected Vault and owner MPToken found semantically despite two-object delta. |
| H2 missing Vault fails | PASS | Validated `tesSUCCESS` cannot override missing Vault state. |
| H3 missing/wrong owner MPToken fails | PASS | Both required cases fail. |
| H4 unrelated extra object allowed | PASS | Global count is not the oracle. |
| H5 object order irrelevant | PASS | Reversed committed owner-object list passes. |
| H6 wrong Vault owner fails | PASS | Owner relationship is required. |
| H7 wrong Vault asset fails | PASS | tRLUSD currency/issuer identity is required. |
| H8 success with wrong post-state fails | PASS | `tesSUCCESS` alone cannot pass. |
| H9 committed `tecEXPIRED` still passes | PASS | Submission result survives the unvalidated lookup and protected state is equal. |
| H10 negative with mutated state fails | PASS | Expected rejection alone is insufficient. |
| H11 unrun statuses remain distinct | PASS | Serialization and aggregation preserve `NOT_ATTEMPTED` versus `NOT_APPLICABLE`. |
| H12 manifest tamper detected | PASS | Untampered copy verifies; one-byte mutation is rejected. |

The spike suite reports 35 passing test cases: H1-H12 (with both H3 cases), four
additional object-helper tests, the earlier result-handling regressions, and
amendment validation regressions.

## Predeclared E3/E4 assertion audit

`STATE-ASSERTIONS.md` defines the pre-state, result, validation status,
post-state, semantic predicates, protected state, permitted auxiliaries, FAIL
conditions, and BLOCKED conditions for all eleven required future steps A-K.

The static audit removed or prevented:

- global owner/ledger object counts;
- list-order identity;
- `tesSUCCESS`-only PASS;
- negative-result overwrite;
- comparison of unrelated auxiliary state; and
- use of transaction-history, directory-position, order, or global-count fields
  as stable identity.

All required A-K object identities are derivable from committed evidence and
pinned source. No assertion specification is marked unresolved.

## Remaining harness risks

- The hardened E3/E4 oracles have not yet been exercised by a new runtime run.
- LoanBroker and cover helpers are source/synthetic-derived because E4 has never
  run; they must still fail closed against the first authorized observation.
- An ambiguous semantic match fails rather than choosing an object by order.
- Only explicitly known default-zero fields are normalized when absent.
- LoanSet and LoanPay helpers remain deliberately unimplemented.

## Results state and recommendation

- E1: **PASS/FROZEN**
- E2: **PASS/FROZEN**
- E3: **BLOCKED**
- E4-E9: **NOT_ATTEMPTED**

The harness is ready for a **separately authorized E3/E4 rerun**. This is not an
authorization to resume E3 or execute E4-E9. E5 must not begin.

Harness verdict: **PASS WITH CONDITIONS**. The conditions are that the future
E3/E4 run is separately authorized, uses the predeclared oracle unchanged unless
new evidence forces a fail-closed STOP, and does not treat the source/synthetic
E4 helpers as runtime observations.

## Checkpoint 1C-H gates

- Spike-specific tests: PASS — 35 cases.
- Repository unit tests: PASS — 17 cases.
- Conformance tests: PASS — 4 cases.
- Lint, formatting, and large-file check: PASS.
- Strict typecheck: PASS over 25 source files.
- Secrets scan: PASS.
- JSON validation: PASS — 61 files.
- Existing evidence manifests: PASS — Checkpoints 1B, 1B-R, 1C, and 1C-R.
- Authorized-path check: PASS — only `spike/xrpl/` changed.
- `review/xrpl/`, web, and custody: unchanged.
- New transaction/evidence paths: none.
- `git diff --check`: PASS.

## Checkpoint 1C-H modified paths

- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`
- `spike/xrpl/STATE-ASSERTIONS.md`
- `spike/xrpl/scripts/harness.py`
- `spike/xrpl/scripts/run_checkpoint_1c_r.py`
- `spike/xrpl/tests/test_state_assertions.py`

---

# Checkpoint 1C-R fail-closed report

## Outcome

**BLOCKED — local post-state assertion error.**

The single authorized fresh run stopped with the exact output:

```text
STOP: e3_create_closed_ended_vault: expected tesSUCCESS, observed tesSUCCESS; result or state assertion failed
```

No retry, assertion correction, environment change, alternate build, additional
amendment, E3 phase operation, E4 operation, or E5-E9 operation was attempted.

## Start state and scope

- Repository `HEAD` began at
  `b366efed4163b729adc4c1fb5d94d7947fdd0385` with a clean worktree.
- That commit descends from peeled `checkpoint-0^{}` commit
  `810c7870d2ebb23e0b07b6017c4c88344f742aed`.
- The custody gitlink remained
  `5dc15ff6fb097dc5f4a47e72969fae4a296620a5`; the web gitlink remained
  `dd47344df9966b5906c0ee13426955e39c0db9cd`.
- Only `spike/xrpl/` changed.
- E1 and E2 remained frozen. Fresh-ledger actor and tRLUSD setup was performed
  only as an E3/E4 prerequisite fixture, not as an E2 rerun or new E2 result.

## Negative-result root cause and repair

The committed Checkpoint 1C harness copied the submission result into a
preliminary variable, closed a ledger for every `tes` or `tec` prefix, then
unconditionally replaced that variable with `meta.TransactionResult` from a
transaction lookup. The E3 Investment deposit submission returned the expected
`tecEXPIRED`, but `fail_hard` left the transaction outside the validated ledger;
the lookup therefore returned `validated: false` with no metadata. The harness
replaced `tecEXPIRED` with an empty value and emitted a false STOP.

`scripts/harness.py` now keeps these observations separate:

- `submission_result`;
- `submission_hash_if_any`;
- `validated_lookup_status`;
- `validated_result_if_any`;
- `before_state` and `after_state`;
- `expected_result`; and
- `state_assertion` plus the individual state assertions.

An unvalidated or absent lookup cannot overwrite a submission result. A positive
case requires matching validated metadata and asserted post-state. A deterministic
negative requires the expected result and unchanged relevant ledger state.
Client exceptions, malformed requests, and transport/server failures have
separate categories.

## Regression tests

All eight required result-handling regressions passed before the ledger run:

| Test | Result |
| --- | --- |
| R1 `tecEXPIRED` survives an unvalidated lookup | PASS |
| R2 expected deterministic negative plus unchanged-state assertions | PASS |
| R3 `tesSUCCESS` cannot pass without validated post-state | PASS |
| R4 client-side exception remains distinct | PASS |
| R5 absent lookup does not overwrite submission | PASS |
| R6 unexpected engine result fails | PASS |
| R7 `NOT_ATTEMPTED` differs from `NOT_APPLICABLE` | PASS |
| R8 committed Checkpoint 1C evidence reclassifies correctly | PASS |

Together with ten amendment tests, the spike test command reported **18 passed**.

## Frozen results

- **E1: PASS/FROZEN.** The fresh prerequisite environment reported xrpld 3.4.0,
  network ID 21337, fresh validated ledger 2, and all eight frozen required
  amendments present, supported, and enabled. This was an invariant cross-check,
  not a new E1 experiment.
- **E2: PASS/FROZEN.** The Checkpoint 1C E2 result was not reinterpreted or
  replaced. The fresh fixture reached validated ledger 12 with lender balance
  200000 tRLUSD, borrower balance 0, and Loan Broker owner balance 20000 tRLUSD.

## E3 result and exact stop

**E3: BLOCKED.** The first E3 transaction created a closed-ended tRLUSD Vault.
Both submission and validated metadata reported `tesSUCCESS` at ledger 13,
transaction
`7F2F22090B6A120B5E404A6D985D337DA00E791D6D9E95007938FFE844069D79`.
The validated object contained VaultKind 1, SubscriptionDate 845082260, and
RedemptionDate 845082440.

The post-state assertion incorrectly expected the Vault owner's `account_objects`
count to increase by one. It increased by two: the validated post-state added a
`Vault` and the owner's `MPToken`; the existing `RippleState` remained present.
The transaction metadata also recorded creation of the Vault, share
`MPTokenIssuance`, owner `MPToken`, Vault pseudo-account, and related directory
state. This is **A — local configuration/harness assertion error**, not a
protocol failure. The result resolver correctly retained the validated
`tesSUCCESS`; the new negative-result repair did not regress.

The fail-closed assertion still required STOP. Therefore:

- E3 Subscription deposit: **NOT ATTEMPTED**;
- E3 Subscription withdrawal: **NOT ATTEMPTED**;
- E3 Investment entry, deposit, and withdrawal: **NOT ATTEMPTED**;
- E3 Redemption entry, deposit, and withdrawal: **NOT ATTEMPTED**;
- no phase timing was observed beyond creation at close time 845082231;
- loan origination was not exercised.

## E4 result and cover classification

**E4: NOT ATTEMPTED.** STOP occurred during the first E3 transaction. No fresh E4
Vault, LoanBrokerSet, negative ownership test, Loan Broker object, or cover
deposit was submitted. Consequently:

- ownership relationship: source-derived only, not observed in this run;
- cover amount, rates, and limits: not observed;
- **OBSERVED:** no E4 ledger finding;
- **DERIVED:** pinned source requires LoanBrokerSet `Account` to equal the Vault
  `Owner`, and bounds default cover using debt/rate/default/available-cover state;
- **UNRESOLVED:** cover establishment, default/loss behavior, clawback,
  liquidation, and realized-loss accounting.

The proposed E4 configuration values (10000 cover deposit, 10000 minimum cover
rate, 25000 liquidation cover rate, and 50000 debt maximum) were not submitted
and are not observations.

## Proposed plan corrections

No normative build-plan file was edited. Before a separately authorized rerun,
the spike plan should require VaultCreate post-state to assert ledger-entry types
and identities rather than a total owner-object delta of one. The expected
creation set must account for the owner `MPToken` in addition to the `Vault`,
while independently validating the share issuance and pseudo-account from
metadata or direct lookup. This is a spike-harness correction, not a Credence
architecture correction.

The minimum next experiment is an E3/E4-only fresh run after changing only that
VaultCreate post-state assertion and adding a regression fixture for the observed
two-object owner-list delta. That experiment was **not performed**.

## Evidence manifest

`evidence/checkpoint-1c-r/MANIFEST.json` covers 18 files totaling 191588 bytes
with SHA-256. It includes the fresh environment, public actor addresses, setup
state, 11 transaction records, 11 ledger closes, pinned-source findings, and the
exact unexpected result. Signing material, transaction blobs, seeds, and private
keys are absent.

## Experiment status

| Experiment | Status | Reason |
| --- | --- | --- |
| E1 | PASS/FROZEN | Frozen environment invariants cross-checked only. |
| E2 | PASS/FROZEN | Prior result retained; fresh setup was prerequisite-only. |
| E3 | BLOCKED | Valid VaultCreate contradicted a local owner-object-count assertion. |
| E4 | NOT_ATTEMPTED | STOP occurred before E4. |
| E5 | NOT_ATTEMPTED | Outside scope; STOP remained in force. |
| E6 | NOT_ATTEMPTED | Outside scope; STOP remained in force. |
| E7 | NOT_ATTEMPTED | Outside scope; STOP remained in force. |
| E8 | NOT_ATTEMPTED | Outside scope; STOP remained in force. |
| E9 | NOT_ATTEMPTED | Outside scope; STOP remained in force. |

## Recommendation

**BLOCKED. E5 must not begin.** Adjudicate a narrowly authorized E3/E4 recovery
that corrects only the demonstrated VaultCreate post-state assertion. Do not
interpret the successful VaultCreate as an E3 phase-lifecycle PASS or an E4 PASS.

## Gates

- Spike tests: PASS — 18 tests, including R1-R8 and ten amendment tests.
- Repository unit tests: PASS — 17 tests.
- Conformance tests: PASS — 4 tests.
- Lint and formatting: PASS.
- Large-file check: PASS.
- Strict typecheck: PASS over 25 source files.
- Secrets scan: PASS after public evidence omitted the server node public key
  and the redundant name-to-amendment-ID cross-check map; no signing material or
  secret was removed because neither field was signing material or a secret.
- JSON validation: PASS — 61 files.
- Evidence-manifest verification: PASS — 18 files, no digest or size mismatch.
- Authorized-path check: PASS — only `spike/xrpl/` changed.
- `git diff --check`: PASS.

## Modified paths

- `spike/xrpl/ENVIRONMENT.md`
- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`
- `spike/xrpl/scripts/harness.py`
- `spike/xrpl/scripts/run_checkpoint_1c_r.py`
- `spike/xrpl/tests/test_harness.py`
- `spike/xrpl/evidence/checkpoint-1c-r/`

---

# Checkpoint 1 fail-closed report

## Outcome

**BLOCKED — local validation-harness error.**

The spike stopped before the first protocol transaction with the exact output:

```text
STOP: Required network/amendment state is absent
```

No build was switched, no amendment was added, no configuration was changed
after the stop, and no workaround or downstream experiment was attempted.

## OBSERVED

### Start state

- `HEAD` began exactly at peeled annotated tag `checkpoint-0^{}` commit
  `810c7870d2ebb23e0b07b6017c4c88344f742aed`.
- The custody submodule gitlink was
  `5dc15ff6fb097dc5f4a47e72969fae4a296620a5`.
- The web submodule gitlink was
  `dd47344df9966b5906c0ee13426955e39c0db9cd`.
- All writes made for the spike are under `spike/xrpl/`.

### Exact server and build

The stopped server reported `xrpld version 3.4.0`, built from Git commit
`4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`. The installed package is the
XRPLF stable Debian package `xrpld 3.4.0-1 amd64`; its downloaded artifact was
verified against SHA-256
`4dc40a21b12b7d21f2760b16545c1d842962a96978dd3199eb8de9aa0aff8339`.
Full provenance is in `ENVIRONMENT.md`.

### Configuration and genesis

The server ran `--standalone --start` with the checked-in
`config/xrpld.cfg`, network ID `21337`, and a new empty named data volume. No
custom genesis file was supplied. The stopped server was `full`, with complete
ledger range `2` and validated ledger index 2. No protocol transaction was
submitted and no ledger-advance RPC was called by the failed run.

### Complete amendment state

The unchanged server's read-only `feature` response contained 106 amendments:
9 enabled and 97 disabled. The complete ID/name/supported/enabled/vetoed list is
preserved in `evidence/amendments.json`.

Every amendment required by the spike was present, supported, and enabled:

| Amendment | Supported | Enabled |
| --- | --- | --- |
| MPTokensV1 | yes | yes |
| SingleAssetVault | yes | yes |
| LendingProtocol | yes | yes |
| LendingProtocolV1_1 | yes | yes |
| fixCleanup3_1_3 | yes | yes |
| fixCleanup3_2_0 | yes | yes |
| fixCleanup3_3_0 | yes | yes |
| fixCleanup3_4_0 | yes | yes |

The ninth enabled amendment was `fixRemoveNFTokenAutoTrustLine`.

### What the gate reported absent

The gate reported all eight required amendments as absent. That observation is
preserved verbatim as structured data in `evidence/unexpected-result.json`.
It did not report the network ID absent: the observed network ID was the
required `21337`.

The post-stop read-only audit found that **no required network or amendment
state was actually absent**. The false negative had two local causes:

1. The `feature` RPC helper returned the result object whose amendment map is
   nested under `features`, but the gate looked up IDs directly in that result
   object.
2. Each of the eight amendment constants in the gate differed from the exact
   ID present in the pinned build, configuration, and RPC response.

Because a missing lookup defaulted to an empty object, all eight `enabled`
checks evaluated false and the fail-closed guard fired before any transaction.

### Presence in the pinned release

All required amendments exist in release `3.4.0`. This is independently
supported by two observations:

- the pinned release source declares all eight in
  `include/xrpl/protocol/detail/features.macro`; and
- the running 3.4.0 binary's `feature` RPC lists all eight with
  `supported: true`.

No development build or patch was required to make them appear.

## DERIVED

### Failure classification

**A. local configuration error**, specifically a local validation-harness
configuration/parsing error.

The original evidence labeled the result `B. amendment unavailable/not
enabled` because that was the fail-closed classification emitted at the moment
of detection. The preserved server response falsifies classification B: all
eight are available, supported, and enabled. There is no evidence of malformed
protocol transactions (C), misunderstood protocol preconditions (D), an
unsupported authorization composition (E), or a protocol defect (F), because
no protocol transaction was submitted.

This classification does not turn E1-E9 into protocol passes. It establishes
only that the STOP was caused locally and occurred before those experiments.

## UNRESOLVED

- Reproducible explicit ledger closure and persistence during a run were not
  demonstrated after the fresh start.
- tRLUSD issuance and actor funding were not attempted.
- Closed-ended vault phases, Loan Broker cover, borrower 2-of-2, LoanSet
  counterparty authorization, disbursement, LoanPay, and redemption were not
  attempted.
- The central borrower 2-of-2 plus distinct Loan Broker counterparty-signature
  composition remains untested.
- No conclusion can be drawn about compatibility with the Credence Build Plan.

## E1-E9 status

| Experiment | Status | Reason |
| --- | --- | --- |
| E1 server/amendments | BLOCKED | Startup and raw amendment state were observed, but the automated gate failed locally before closure/persistence checks. |
| E2 tRLUSD | NOT_APPLICABLE | Not attempted after STOP. |
| E3 closed-ended vault | NOT_APPLICABLE | Not attempted after STOP. |
| E4 Loan Broker | NOT_APPLICABLE | Not attempted after STOP. |
| E5 borrower 2-of-2 | NOT_APPLICABLE | Not attempted after STOP. |
| E6 LoanSet composition | NOT_APPLICABLE | Not attempted after STOP. |
| E7 disbursement | NOT_APPLICABLE | Not attempted after STOP. |
| E8 LoanPay | NOT_APPLICABLE | Not attempted after STOP. |
| E9 full phase life | NOT_APPLICABLE | Not attempted after STOP. |

## Minimum next experiment — not performed

The minimum experiment that would distinguish the remaining harness question
is to correct only the E1 validator so that it reads the nested `features` map
and uses the exact eight IDs from the pinned 3.4.0 release, then run only that
validator against a fresh standalone ledger created from the identical pinned
image and unchanged `xrpld.cfg`. A passing check would distinguish the local
false negative from a reproducible amendment-state failure without entering E2
or submitting a protocol transaction.

That experiment was **not performed**.

## Recommendation

**BLOCKED.** Preserve this stop and adjudicate whether a new checkpoint run may
make the minimal E1 harness correction. Do not interpret this report as a
protocol PASS or FAIL.

## Tests and gates

- `make test`: PASS — 17 unit tests and 4 conformance tests.
- `make lint`: PASS — Ruff checks, formatting check, and large-file check.
- `make typecheck`: PASS — strict mypy over 25 source files.
- `make secrets`: PASS — no findings after the ignored, temporary upstream
  source checkout used for provenance inspection was moved outside the
  workspace; no repository file was excluded from the scan.
- JSON syntax validation for `RESULTS.json` and all JSON evidence: PASS.
- `git diff --check`: PASS.

## Created or modified paths

- `spike/xrpl/README.md`
- `spike/xrpl/ENVIRONMENT.md`
- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`
- `spike/xrpl/config/Dockerfile`
- `spike/xrpl/config/xrpld.cfg`
- `spike/xrpl/evidence/amendments.json`
- `spike/xrpl/evidence/server-info.json`
- `spike/xrpl/evidence/unexpected-result.json`
- `spike/xrpl/scripts/run_experiments.py`

---

# Checkpoint 1A recovery

## Original failure

Checkpoint 1 stopped at E1 with:

```text
STOP: Required network/amendment state is absent
```

Checkpoint 1A reproduced that false result without submitting a transaction:
the old shallow lookup interpreted all eight required amendments as disabled
when run against the actual `xrpld` 3.4.0 `feature` response.

## Root cause

The validator made two exact local assumptions that were false:

1. It treated amendment IDs as direct children of the `result` object. The RPC
   shape is `result.features.<amendment-id>`.
2. Its eight configured amendment IDs did not match the IDs reported by the
   pinned 3.4.0 build and already present in `config/xrpld.cfg`.

The missing lookups defaulted to empty objects, so every `enabled` check became
false even though all eight amendments existed, were supported, and were
enabled.

## Exact fix

`scripts/amendment_validation.py` now owns the exact eight IDs and validates a
complete JSON-RPC response through `result.features`. For each required entry,
it fails closed unless:

- the exact ID exists;
- the reported name matches;
- `supported` is exactly `true`; and
- `enabled` is exactly `true`.

`scripts/run_experiments.py` now delegates its amendment gate to that strict
validator. `scripts/validate_environment.py` is a separate E1-only runner; it
cannot enter E2.

## Regression test

`tests/test_amendment_validation.py` contains six passing tests against the
captured real 3.4.0 response. They prove that:

- the real response passes;
- the original shallow-result lookup reproduces the false BLOCKED result;
- a genuinely missing required amendment fails;
- a disabled required amendment fails;
- an unsupported required amendment fails; and
- a missing nested `result.features` map fails.

Spike-specific result: **6 passed**.

## Raw evidence

The E1 runner preserved the complete raw `feature` responses independently of
the parser output:

| Capture | SHA-256 | Bytes |
| --- | --- | ---: |
| Regression fixture | `73e1a3250c4c37c4be085273f9d477654d8344d083ec3bab9eaff72b3209f1e7` | 15190 |
| Before closes | `3f9caa62f7b9c340207c49ed6f16fadbd605bb8b068332a4f358957f5ece3cd8` | 15192 |
| After closes | `3f9caa62f7b9c340207c49ed6f16fadbd605bb8b068332a4f358957f5ece3cd8` | 15192 |

The identical raw digests agree with the validator's before/after output. A
reviewer can independently parse `evidence/feature-raw-e1-before.json` and
`evidence/feature-raw-e1-after.json`. The complete machine-readable result is
`evidence/e1-validation.json`.

## Corrected E1 result

**PASS.** Observed evidence establishes:

- `xrpld` 3.4.0 from source commit
  `4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`;
- Debian package `xrpld 3.4.0-1 amd64` and the previously pinned binary/image
  digests;
- standalone mode with network ID `21337`;
- two successful, reproducible closes from validated ledger 2 to 3 to 4;
- all eight required amendments exist, are supported, and are enabled;
- the same required state survived both closes; and
- the strict validator agrees with both raw responses.

No transaction was submitted. E2-E9 were not executed.

## Previous BLOCKED conclusion

The previous E1 **BLOCKED** conclusion is **retired** because its sole cause was
the corrected local validation defect. The historical STOP and classification
remain preserved above and in `evidence/unexpected-result.json`; they are not
rewritten as a protocol result.

## Checkpoint 1A recommendation

**PASS.** E1 no longer blocks the spike. E2 may begin in a separately authorized
continuation from this commit. Checkpoint 1A did not begin E2.

## Checkpoint 1A gates

- Spike-specific regression tests: PASS — 6 tests.
- Repository tests: PASS — 17 unit and 4 conformance tests.
- Lint and formatting: PASS.
- Typecheck: PASS — strict mypy over 25 source files.
- Secrets scan: PASS — no findings.
- JSON validation and `git diff --check`: PASS.
- Authorized-path check: PASS — every Checkpoint 1A change is under
  `spike/xrpl/`.

## Checkpoint 1A files changed

- `spike/xrpl/.gitattributes`
- `spike/xrpl/ENVIRONMENT.md`
- `spike/xrpl/README.md`
- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`
- `spike/xrpl/evidence/e1-validation.json`
- `spike/xrpl/evidence/feature-raw-e1-after.json`
- `spike/xrpl/evidence/feature-raw-e1-before.json`
- `spike/xrpl/evidence/feature-raw-original.json`
- `spike/xrpl/scripts/amendment_validation.py`
- `spike/xrpl/scripts/run_experiments.py`
- `spike/xrpl/scripts/validate_environment.py`
- `spike/xrpl/tests/test_amendment_validation.py`

---

# Checkpoint 1C fail-closed report

## Outcome

**BLOCKED — local negative-result handling defect during E3 Investment.**

The server's submission response for the Investment-phase deposit contained
`engine_result: tecEXPIRED`, exactly matching the source-derived expectation,
and every relevant state-unchanged assertion passed. Because `fail_hard` left
the rejected transaction unvalidated, the subsequent `tx` lookup returned
`validated: false` without metadata. The harness incorrectly replaced the
preliminary `tecEXPIRED` with an empty result and stopped with:

```text
STOP: investment_deposit_rejected: expected tecEXPIRED, observed None; result or state assertion failed
```

This is **A. local configuration error** in result handling, not evidence of a
vault-phase contradiction. No timing adjustment, retry, Investment withdrawal,
Redemption operation, or E4 transaction followed.

## E2 — OBSERVED

- Four disposable AccountRoot objects exist: tRLUSD issuer, lender, borrower,
  and Credence Loan Broker account. Only public classic addresses are recorded.
- The issuer has DefaultRipple enabled.
- Lender, borrower, and Loan Broker trust lines use XRPL wire currency
  `74524C5553440000000000000000000000000000`, the six-byte label `tRLUSD`.
- Validated wire balances are lender `200000`, borrower `0`, and Loan Broker
  `20000`.
- An absent, unfunded actor could not autofill the required TrustSet; the client
  returned `actNotFound`, and the actor remained absent.
- Every successful state-changing transaction has validated before/after state
  and passing assertions; E2 is **PASS**.

## E2 — DERIVED

At the Credence application-accounting convention of 1,000,000 integer
micro-tRLUSD per tRLUSD, the lender's `200000` XRPL amount would correspond to
`200000000000` application micro-units. This is arithmetic interpretation only;
Checkpoint 1C does not define a production conversion or repayment algorithm.

## E2 — UNRESOLVED

- Production precision, rounding, and repayment conversion remain deferred.
- Borrower signer configuration remains entirely deferred to E5.

## E3 — OBSERVED

- A closed-ended tRLUSD Vault was created with the Credence Loan Broker account
  as owner, `VaultKind: 1`, SubscriptionDate `845081395`, RedemptionDate
  `845081575`, Scale `6`, and WithdrawalPolicy `1`.
- In Subscription, a `100000` deposit succeeded: vault total/available assets
  increased, lender tRLUSD decreased, and lender MPT shares equaled issuance
  outstanding shares.
- In Subscription, a `1000` withdrawal succeeded with the inverse asset,
  balance, and share transitions.
- Forty-three scripted closes moved the parent close time across the
  Subscription boundary. Investment was observed at ledger 58, close time
  `845081396`, one second after SubscriptionDate. No wall-clock sleep was used.
- An Investment deposit of `1` produced server engine result `tecEXPIRED`.
  AssetsTotal, AssetsAvailable, lender balance, share issuance, and lender share
  balance were unchanged.

E3 remains **BLOCKED**, rather than PASS, because the harness failed to retain
that correct preliminary engine result when the transaction was not validated.

## E3 — DERIVED

The observed Subscription behavior and Investment deposit rejection agree with
pinned source semantics. Loan-origination phase restrictions remain
source-derived because LoanSet was prohibited.

## E3 — UNRESOLVED

- Investment withdrawal was not attempted after STOP.
- Redemption deposit and withdrawal were not attempted after STOP.
- The full three-phase matrix and fresh-run reproducibility therefore remain
  incomplete.

## E4 — OBSERVED

None. E4 was **NOT_ATTEMPTED** because STOP occurred during E3.

## E4 — DERIVED

Pinned source continues to indicate bounded protection controlled by
CoverAvailable, CoverRateMinimum, CoverRateLiquidation, debt/default amounts,
and the Loan Broker/vault relationship. No E4 state was created, so none of
those semantics are upgraded to observed evidence.

## E4 — UNRESOLVED

Loan Broker ownership, vault relationship, cover amount/rates/limits, the
unauthorized-operation negative case, and all before/after cover state remain
untested.

## PLAN CORRECTIONS INDICATED BY EVIDENCE

- Preserve the existing proposed correction that first-loss wording must be
  qualified by configured cover bounds. Checkpoint 1C produced no E4 evidence
  that would justify stronger language.
- Keep XRPL decimal wire amounts distinct from integer application
  micro-tRLUSD. A production conversion and rounding rule is still required.
- Do not describe all three vault phases as experimentally validated; only
  Subscription and the Investment deposit rejection have run evidence here.

The normative plan was not modified.

## Minimum next experiment — not performed

Change only negative-result handling so an `accepted: false` `tec` result is
retained when the subsequent `tx` lookup is unvalidated, then restart from a
fresh volume and rerun E2-E4. That distinguishes the local evidence-harness
defect from any remaining phase or broker question.

That experiment was **not performed**.

## Recommendation

**BLOCKED. E5 must not begin.** Complete E3 and E4 from a fresh run after a
narrowly authorized harness repair.

## Checkpoint 1C STOP gates

- Existing spike tests: PASS — 10 tests.
- Repository unit tests: PASS — 17 tests.
- Conformance tests: PASS — 4 tests.
- Lint, formatting, large-file check, and strict typecheck: PASS.
- Secrets scan: PASS — no findings.
- JSON validation, SHA-256 evidence-manifest verification, authorized-path
  check, and `git diff --check`: PASS.
- Review path, web gitlink, and custody gitlink: unchanged.

The candidate E2-E4 runner was removed after STOP and is not committed as a
completed harness. The commit contains only the bounded report and evidence
artifacts under `spike/xrpl/`.

---

# Checkpoint 1B fail-closed report

## Outcome

**BLOCKED — E1 regressed in the hardened validation harness.**

The fresh pinned server started successfully, and the existing strict `feature`
validator again accepted all eight required amendments. The newly added
on-ledger check then sent `ledger_entry` with `amendments: {}`. rippled returned:

```text
malformedRequest — Invalid field 'amendments', not hex string.
```

This matched the checkpoint's explicit `E1 no longer passes` STOP condition.
No retry, selector correction, ledger close, or E2-E9 transaction followed.

## OBSERVED

- Start-state `HEAD` was exactly
  `d4eb8743cdba87f8cdc13df454d0f1a5df1e4dd1`, descended from
  `checkpoint-0^{}` / `810c7870d2ebb23e0b07b6017c4c88344f742aed`.
- The worktree was clean. Custody remained at `5dc15ff6...`; web remained at
  `dd47344...`.
- Review commit `89fa0a63de502fc03402afb70af273b923415ea4` was inspected read-only;
  `review/xrpl/` was not merged, cherry-picked, or modified.
- The checked-in pinned build produced `xrpld version 3.4.0`, source commit
  `4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`.
- The dedicated volume was recreated and mounted at `/var/lib/xrpld`, matching
  the checked-in database path. The invocation retained `--standalone --start`,
  network ID `21337`, and the unchanged configuration hash
  `37ffc6f93f7d8838aef681e4a3c7e39d7427e101cb6c62113dca208dc1e8e820`.
- At STOP the fresh ledger was index 2, close time `845019610`, and no protocol
  transaction had been submitted.
- The pre-failure raw `feature` response has SHA-256
  `3f9caa62f7b9c340207c49ed6f16fadbd605bb8b068332a4f358957f5ece3cd8`.

## DERIVED

The failure is **A. local configuration error**, specifically a request-shape
defect in the new E1 harness. Pinned 3.4.0 source shows the fixed-location
Amendments selector accepts boolean `true` (or a valid object index), not an
empty object. This source observation explains the response; it was not used to
retry the call or upgrade E1 to PASS.

The failure is not evidence that any required amendment is absent, and it is
not a protocol-transaction failure. The previous Checkpoint 1A E1 PASS remains
historical evidence, but the 1B regression gate is BLOCKED.

## UNRESOLVED

- The new comparison between `feature` and the Amendments ledger object was not
  completed.
- The post-hardening two-close E1 regression proof was not run.
- E2-E5 were not attempted, so no tRLUSD, vault, broker, cover, signer-list, or
  ordinary Payment claim was observed.
- E6-E9 remain NOT_ATTEMPTED by scope.

## Independent-review findings addressed

| Finding | Disposition at STOP | Evidence |
| --- | --- | --- |
| H1 state assertions required | BLOCKING | Harness unit tests passed, but E1 stopped before transaction execution. The uncommitted implementation was not treated as experiment evidence. |
| H2 misleading master-signed LoanPay | DEFERRED TO E6+ | E8 was not executed; no LoanPay evidence was produced. |
| H3 structured client exceptions | BLOCKING | The harness unit test passed, but the E1 runner itself still surfaced this RPC error as an exception and triggered STOP. |
| H4 signer evidence | NOT_APPLICABLE TO 1B RUN | No signer transaction was submitted. |
| H5 database/mount mismatch | FIXED FOR OBSERVED RUN | Docker inspection showed the named volume at `/var/lib/xrpld`. |
| H6 checked-in run command | BLOCKING | A candidate script started the exact environment, but the checkpoint stopped before it could be accepted and committed as completed hardening. |
| H7 self-contained `--start` environment | FIXED FOR OBSERVED RUN | The fresh dedicated spike container used `--standalone --start`; no infra path changed. |
| H8 explicit result states | FIXED | `RESULTS.json` uses BLOCKED and NOT_ATTEMPTED distinctly; no NOT_APPLICABLE substitution. |

## Experiment status

| Experiment | Status | Reason |
| --- | --- | --- |
| E1 | BLOCKED | New on-ledger amendment RPC used an invalid selector shape. |
| E2 | NOT_ATTEMPTED | STOP occurred before the first protocol transaction. |
| E3 | NOT_ATTEMPTED | STOP occurred before the first protocol transaction. |
| E4 | NOT_ATTEMPTED | STOP occurred before the first protocol transaction. |
| E5 | NOT_ATTEMPTED | STOP occurred before the first protocol transaction. |
| E6 | NOT_ATTEMPTED | Outside scope and prohibited. |
| E7 | NOT_ATTEMPTED | Outside scope and prohibited. |
| E8 | NOT_ATTEMPTED | Outside scope and prohibited. |
| E9 | NOT_ATTEMPTED | Outside scope and prohibited. |

## Minimum next experiment — not performed

Change only the fixed-location Amendments `ledger_entry` selector from an
object to the pinned release's accepted boolean form. Then run only E1 from a
newly recreated dedicated volume and compare the `feature` response with the
returned Amendments ledger object. This would distinguish the local request
defect from any remaining ledger-state discrepancy.

That experiment was **not performed**.

## Recommendation

**BLOCKED. E6 must not begin.** Adjudicate a narrowly authorized E1 harness
correction and rerun before E2-E5, and require E2-E5 to pass before E6.

## Checkpoint 1B gates

- Existing spike-specific amendment tests: PASS — 6 tests.
- Repository unit tests: PASS — 17 tests.
- Conformance tests: PASS — 4 tests.
- Lint, format check, and large-file check: PASS.
- Strict typecheck: PASS — 25 source files.
- Secrets scan: PASS — no findings.
- JSON syntax, evidence-manifest verification, and `git diff --check`: PASS.
- Authorized-path check: PASS — every change is under `spike/xrpl/`.
- Review path and both submodule gitlinks: unchanged.

The hardened-harness unit tests had passed before the E1 run, but the candidate
implementation was removed after STOP and is not part of this commit. It is
therefore not reported as a completed gate or fix.

## Checkpoint 1B modified paths

- `spike/xrpl/ENVIRONMENT.md`
- `spike/xrpl/README.md`
- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`
- `spike/xrpl/evidence/checkpoint-1b/MANIFEST.json`
- `spike/xrpl/evidence/checkpoint-1b/feature-raw-e1-before.json`
- `spike/xrpl/evidence/checkpoint-1b/unexpected-result.json`

---

# Checkpoint 1B-R Amendments request repair

## Outcome

**PASS — E1 restored and independently cross-checked.**

No E2-E9 transaction was submitted.

## OBSERVED

Pinned rippled 3.4.0 source implements Amendments as a fixed-location ledger
entry. `src/xrpld/rpc/handlers/ledger/LedgerEntry.cpp` routes it through
`fixed()` and `parseFixed()`. That parser accepts boolean `true`; otherwise it
tries to parse the supplied value as a ledger-object index.

The pinned source's `src/test/rpc/LedgerEntry_test.cpp` explicitly tests:

```json
{"ledger_index":"validated","amendments":true}
```

The prior `{}` selector was therefore invalid: it was neither boolean `true`
nor a valid object-index string, producing the recorded `malformedRequest`.

The repaired E1 run recreated the dedicated container and volume, mounted the
volume at `/var/lib/xrpld`, and started the unchanged pinned build with
`--standalone --start`. Observed build identity was xrpld `3.4.0`, package
`xrpld 3.4.0-1 amd64`, source commit
`4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`, and binary SHA-256
`9b4d3dd28b31a22419fd905682e238298eefb8941e2efcde5241a4b19fa0c417`.

The validated ledger advanced exactly from index 2 to 3 to 4. Close times were
`845080860`, `845080861`, and `845080862`.

Before and after those closes:

- `feature` contained 106 amendments and reported all eight required entries
  with the exact name and ID, `supported: true`, and `enabled: true`;
- the Amendments ledger object contained nine enabled IDs, including all eight
  required IDs; and
- the cross-check reported no disagreement for any required amendment.

Raw responses and their SHA-256 manifest are under
`evidence/checkpoint-1b-r/`.

## DERIVED

The Checkpoint 1B stop remains correctly classified as **A. local
configuration error** in the validation harness. The corrected request and two
independent representations retire that stop without changing the pinned
server, amendments, or configuration.

## UNRESOLVED

E2-E9 remain NOT_ATTEMPTED. This checkpoint establishes only E1 environment
invariants; it makes no tRLUSD, vault, broker, cover, signer, LoanSet, LoanPay,
or lifecycle claim.

## Regression tests

Ten spike tests pass. They cover the real 3.4.0 feature fixture, reject the
previous `{}` selector locally, accept `amendments: true`, validate a returned
Amendments object, cross-check both representations, and fail for missing,
disabled, or unsupported required amendments.

## E1 freeze

E1 environment validation is frozen after Checkpoint 1B-R. Later checkpoints
may verify its recorded invariants but must not add new E1 requirements without
a separately approved checkpoint.

## Recommendation

**PASS. Checkpoint 1B may resume at E2.** E6-E9 remain prohibited until their
separately authorized checkpoints.

## Checkpoint 1B-R gates

- Spike-specific tests: PASS — 10 tests.
- Repository unit tests: PASS — 17 tests.
- Conformance tests: PASS — 4 tests.
- Lint, format check, and large-file check: PASS.
- Strict typecheck: PASS — 25 source files.
- Secrets scan: PASS — no findings. Public node-key fields irrelevant to E1
  were deliberately omitted from captured server responses under the repository
  no-key-shaped-strings rule; each evidence file records the omission.
- JSON syntax, evidence-manifest verification, and `git diff --check`: PASS.
- Authorized-path check: PASS — every change is under `spike/xrpl/`.
- Review path and both submodule gitlinks: unchanged.

## Checkpoint 1B-R modified paths

- `spike/xrpl/ENVIRONMENT.md`
- `spike/xrpl/README.md`
- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`
- `spike/xrpl/evidence/checkpoint-1b-r/MANIFEST.json`
- `spike/xrpl/evidence/checkpoint-1b-r/amendments-ledger-entry-after.json`
- `spike/xrpl/evidence/checkpoint-1b-r/amendments-ledger-entry-before.json`
- `spike/xrpl/evidence/checkpoint-1b-r/e1-validation.json`
- `spike/xrpl/evidence/checkpoint-1b-r/feature-after.json`
- `spike/xrpl/evidence/checkpoint-1b-r/feature-before.json`
- `spike/xrpl/evidence/checkpoint-1b-r/server-info-after.json`
- `spike/xrpl/evidence/checkpoint-1b-r/server-info-before.json`
- `spike/xrpl/scripts/amendment_validation.py`
- `spike/xrpl/scripts/start_ledger.sh`
- `spike/xrpl/scripts/validate_environment.py`
- `spike/xrpl/tests/test_amendment_validation.py`
