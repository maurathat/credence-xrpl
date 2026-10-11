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
