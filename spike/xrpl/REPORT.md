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
