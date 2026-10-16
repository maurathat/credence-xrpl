# Checkpoint 1E-NR — E6 negative evidence serialization repair

## Outcome

**PASS — the historical evidence-serialization defect is reproduced offline,
repaired narrowly, and guarded by a full six-case pre-submit preflight. ZERO
XRPL transactions were submitted.**

The checkpoint began at clean commit
`e913fbd1b172455dff0a29c12b890f311f233aad`, descended from checkpoint-0,
with unchanged submodule gitlinks. rippled was not started, XRPL RPC was not
called, and no ledger was advanced.

## Exact root cause and repair

The committed Checkpoint 1E-N N1 construction reproduces the old exception
through the old conversion path:

```text
TypeError: Object of type set is not JSON serializable
```

The exact paths were `state_assertions[4].actual` and
`state_assertions[4].expected` in the `borrower SMT-NUL domains` assertion.
`negative_authorization_assertions` created them respectively from a set
comprehension over borrower signer prefixes and a one-element set literal.
Every N1-N6 case used this construction. The old `json_safe` returned
`asdict(StateAssertion)` without recursively processing that dictionary, and
the old preflight checked assertion truth without serializing a complete case
record. The sets therefore survived until the post-submission `json.dumps`.

The repair deliberately normalizes this unordered string collection at the
assertion boundary to a unique, lexicographically sorted JSON array. The
shared serializer now recursively handles the explicitly supported assertion
and tuple representations, accepts only JSON-native output, and raises with an
exact value path for unsupported types. It does not stringify unsupported
objects. `build_negative_case_record`, `write_json`, and the full preflight all
use this same strict serialization path.

## Regression and preflight results

| Requirement | Result |
| --- | --- |
| S1 committed N1 fixture reproduces the old set failure | **PASS** |
| S2 repaired N1 evidence serializes | **PASS** |
| S3 repaired N2 evidence serializes | **PASS** |
| S4 repaired N3 evidence serializes | **PASS** |
| S5 repaired N4 evidence serializes | **PASS** |
| S6 repaired N5 evidence serializes | **PASS** |
| S7 repaired N6 evidence serializes | **PASS** |
| S8 insertion order produces byte-identical normalized JSON | **PASS** |
| S9 arbitrary objects fail closed | **PASS** |
| S10 bytes fail closed without an explicit encoding rule | **PASS** |
| S11 complete six-case production serialization preflight | **PASS** |
| S12 bypassed normalization fails before the transaction boundary | **PASS** |

The six-case preflight includes protected-state structures, public
authorization/signing structures, result placeholders, phase/timing
structures, experiment/checkpoint result structures, and manifest metadata.
It runs in the static pre-ledger preflight and again over exact prepared case
evidence before a future N1 submission.

## Preserved classifications and semantics

- E1/E2 remain **PASS/FROZEN**.
- E3/E4 remain **PASS/RETIRED** within tested scope.
- E5 remains **PASS**.
- Historical E6 remains **FAIL**.
- Positive authorization composition remains **SUPPORTED**.
- Timing remains **SOURCE_CONFORMANT**; the historical timing oracle remains
  **ORACLE_DEFECT**.
- N1 remains **BLOCKED_AFTER_SUBMISSION**. No result or post-state is inferred.
- N2-N6 remain **NOT_ATTEMPTED**.
- E6 authorization architecture remains **NOT_VALIDATED**.
- E7-E9 remain **NOT_ATTEMPTED**.

The N1-N6 expected results, altered-principal construction, timing v2,
borrower and broker signing domains, protected-state definitions, and positive
historical classifications are unchanged.

## Historical evidence and prohibited execution

The historical evidence directories `checkpoint-1e-live-01/`,
`checkpoint-1e-r1-corrected/`, `checkpoint-1e-t/`, and `checkpoint-1e-n/`
are byte-for-byte unchanged from the start commit. No new evidence directory
was created. A future separately authorized run must use a new destination
such as `checkpoint-1e-n-live-02/`.

This checkpoint started no rippled process, made no XRPL RPC call, advanced no
ledger, and submitted no prerequisite, LoanSet, N1-N6, LoanPay, or E7-E9
transaction.

## Gates

- S1-S12: **PASS** (12).
- Prospective E6-negative tests: **PASS** (14).
- Existing E6 tests: **PASS** (40).
- All spike tests: **PASS** (168).
- Repository unit tests: **PASS** (17).
- Conformance tests: **PASS** (4).
- Lint, formatting, large-file check, strict typecheck, secrets scan, JSON
  validation, all manifests, historical-evidence immutability, authorized
  paths, and `git diff --check`: **PASS**.

## Modified paths

- `spike/xrpl/E6-EVIDENCE-SERIALIZATION.md`
- `spike/xrpl/scripts/harness.py`
- `spike/xrpl/scripts/e6_negative_oracle.py`
- `spike/xrpl/scripts/run_checkpoint_1e_n.py`
- `spike/xrpl/tests/test_checkpoint_1e_nr.py`
- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`

---

# Checkpoint 1E-N — E6 negative authorization validation

## Outcome

**STOP / BLOCKED — the first negative reached submission and a ledger close,
but the frozen runner failed while serializing its evidence. No negative is
classified PASS and the E6 authorization architecture is NOT VALIDATED.**

The checkpoint began at clean commit
`543f92d77364700af8f6bfd0b851ffd8924e35de`, descended from checkpoint-0,
with unchanged submodule gitlinks. The historical E6 experiment remains
**FAIL**. Its positive authorization composition remains **SUPPORTED**, its
timing behavior remains **SOURCE_CONFORMANT**, and the historical timing
oracle remains **ORACLE_DEFECT**. These classifications are separate and were
not rewritten.

## Prospective oracle and frozen provenance

`E6-TIMING-ORACLE-v2.md` prospectively defines StartDate as the applying-view
header `closeTime` read during `LoanSet::doApply`, and
`NextPaymentDueDate = StartDate + PaymentInterval`. PaymentInterval and
GracePeriod are each 60 seconds. Transaction lookup `date` is explicitly
forbidden as the StartDate anchor. This applies only after Checkpoint 1E-T and
does not change the historical verdict.

Before xrpld started, the negative-only preflight passed 9 assertions, recorded
zero transactions and `xrpld_started: false`, and froze these hashes:

| Artifact | SHA-256 |
| --- | --- |
| `E6-NEGATIVE-ASSERTIONS.md` | `6c8a89a94a16d1830e99e28acfc2c1fe792ad75cbb5b1050dcea3546e68cf876` |
| `E6-TIMING-ORACLE-v2.md` | `4acb653ab61374757a24800dd05a069a4d942e9fbfc3b9226f2f8d8f7b783f77` |
| `scripts/e6_negative_oracle.py` | `0ac1797f23bd8a62c4e4227a8c7b68c5b9d9504438e68eacb4208455a86b0040` |
| `scripts/run_checkpoint_1e_n.py` | `be7afe654ebc178593be932f5eeb4c430e3fd8e74d38dd2ccd058a089da12319` |
| `tests/test_checkpoint_1e_n.py` | `ed1b1a09897cc4d24f43d0331a6c28bdb1ebb2a1c4c9ce4cbe398181cf0f4167` |

The preflight artifact hash is
`9ce658decccecc20283ed38396a64a10e82bf205b037518759764fce0ebcc5df`.
After setup, the live binding passed all six checks before N1. It bound the
exact N1-N6 finalized constructions, expected results, actor identities,
fixtures, runner, tests, oracle, timing spec, and proof that every expected
Loan was absent. No negative depended on a positive LoanSet.

## Fresh prerequisite state

The recreated environment reported xrpld 3.4.0, pinned source
`4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`, network ID 21337, and all eight
required amendments present, supported, enabled, and cross-checked. It began at
validated ledger 2 with 10-second close-time resolution.

All 21 prerequisite transactions passed. They created the public tRLUSD
fixture, borrower, ResearchBot and Credence policy signers, borrower quorum-2
SignerList, Vault/Loan Broker owner, and two independent Vault/Loan Broker/cover
fixtures. The main fixture entered Investment in ledger 76 at close time
`845486843`. The N6 fixture remained in Subscription at ledger 80, close time
`845486847`, with SubscriptionDate `845486903`. Both expected Loan identities
were absent. No positive LoanSet was submitted as setup.

## STOP at N1

The frozen N1 construction had ResearchBot as the sole borrower signer,
Credence absent, no borrower master signature, and a preflight-validated broker
owner CPT-NUL signature. Its expected result was `tefBAD_QUORUM`.

N1 was submitted. The ledger-close log records the dedicated N1 close from
ledger 80 / `845486847` to ledger 81 / `845486848`. The runner then completed
its post-submission state reads and constructed the frozen assertion list, but
failed before the case record could be persisted:

```text
TypeError: Object of type set is not JSON serializable
```

The exact local defect is that authorization assertions contain set-valued
actual/expected fields. `harness.json_safe` converted each `StateAssertion`
dataclass to a dictionary without recursively normalizing the nested sets.
`json.dumps` therefore raised while writing the N1 transaction record.

The submission response, observed result, and protected-state assertion output
were not persisted. N1 is consequently **BLOCKED_AFTER_SUBMISSION**, not PASS.
No protocol conclusion is drawn from the ledger close alone. Per the frozen
fail-closed rule, the harness was not changed and the run was not retried.

## Case disposition

| Case | Disposition |
| --- | --- |
| N1 | **BLOCKED_AFTER_SUBMISSION** — expected `tefBAD_QUORUM`; observed result and protected-state verdict were not persisted |
| N2 | **NOT_ATTEMPTED** |
| N3 | **NOT_ATTEMPTED** |
| N4 | **NOT_ATTEMPTED** |
| N5 | **NOT_ATTEMPTED** |
| N6 | **NOT_ATTEMPTED** |

No protected-state claim is made for N1. All six pre-submit
authorization/construction bindings remain in `live-bindings.json`, including
their exact finalized-body hashes. Public `SigningPubKey` values were replaced
with an explicit redaction marker during fail-closed packaging to satisfy the
repository's no-key-shaped-strings rule. The bindings do not replace the
missing result and post-state record.

## Architecture and prohibited work

The committed historical positive remains unchanged and retained. No new
positive LoanSet, LoanPay, or E7-E9 transaction was submitted. Because N1 did
not complete classification and N2-N6 were not attempted,
**E6 AUTHORIZATION ARCHITECTURE = NOT VALIDATED** for product integration.
E7-E9 remain **NOT_ATTEMPTED** and must not begin.

## Evidence and immutability

Fail-closed evidence is under `evidence/checkpoint-1e-n/`. Its manifest covers
34 payload files totaling 442,175 bytes and verifies with SHA-256. It contains the
pre-ledger preflight, exact live bindings, environment and prerequisite state,
21 successful setup transaction records, 79 ledger-close records, the generic
exception record, and the stop summary.

Historical evidence directories `checkpoint-1e-live-01/`,
`checkpoint-1e-r1-corrected/`, and `checkpoint-1e-t/` were not modified. Their
manifest hashes remain respectively
`42129b0aad5fb7b6f9e641a600ad848f9d8ef47cae26d7beb3fe65559a81d9c8`,
`af51faa819d13b76646f8291d04eee43460311b77686ce00cbe1bc3177433be5`,
and `07419d73a3d4d4f7e76c06ddaf494dfd006e63c014f4cd24b0771fdf8d731dc1`.

## Gates

- Prospective negative tests: **PASS** (14).
- Existing E6 tests: **PASS** (40).
- All spike tests: **PASS** (156).
- Repository unit tests: **PASS** (17).
- Conformance tests: **PASS** (4).
- Lint, formatting, large-file check, strict typecheck, secrets scan, JSON
  validation, manifests, frozen provenance, historical immutability,
  authorized paths, and `git diff --check`: **PASS**.

## Minimum next work — not performed

A separately authorized harness-only checkpoint must recursively JSON-normalize
`StateAssertion` values and add a regression reproducing this exact set-valued
serialization failure. This checkpoint does not authorize a live retry. That
repair and retry were not performed.

## Modified paths

- `spike/xrpl/E6-NEGATIVE-ASSERTIONS.md`
- `spike/xrpl/E6-TIMING-ORACLE-v2.md`
- `spike/xrpl/scripts/e6_negative_oracle.py`
- `spike/xrpl/scripts/run_checkpoint_1e_n.py`
- `spike/xrpl/tests/test_checkpoint_1e_n.py`
- `spike/xrpl/evidence/checkpoint-1e-n/`
- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`

---

# Checkpoint 1E-T — E6 Loan start-time adjudication

## Outcome

**ORACLE_DEFECT ESTABLISHED; TIMING IS SOURCE_CONFORMANT; AUTHORIZATION
COMPOSITION REMAINS SUPPORTED; HISTORICAL E6 REMAINS FAIL.**

This source-and-committed-evidence checkpoint began at clean commit
`d3612702f97848c2accf921dd9f37510d391d07c`, descended from checkpoint-0
`810c7870d2ebb23e0b07b6017c4c88344f742aed`, with unchanged submodule
gitlinks. It did not start xrpld, call RPC, advance a ledger, or submit a
transaction. N1-N6 and LoanPay were not executed.

## Reconstructed timeline

The table is in execution order. Its time columns are protocol header values,
not a wall-clock event log; the committed evidence contains no submission
timestamp.

| Event | Ledger | Recorded/derived time | Evidence status |
| --- | ---: | ---: | --- |
| Vault SubscriptionDate boundary | 75 | `845357122` | recorded close |
| Investment entry and state immediately before LoanSet | 76 | `845357123` | recorded close; parent of ledger 77 |
| Positive LoanSet submission, Sequence 7 | open 77 | none | submission time not represented |
| LoanSet application while building ledger 77 | 77 | applying header `closeTime = 845357133` | established by pinned source plus persisted `Loan.StartDate` |
| Standalone `ledger_accept` accepts ledger 77 | 77 | final close/transaction date `845357124` | recorded close and transaction lookup |
| Validated Loan schedule | 77 | StartDate `845357133`; next due `845357193` | recorded post-state |

The Vault was configured with SubscriptionDate `845357122` and RedemptionDate
`845357422`. PaymentInterval and GracePeriod were each 60 seconds. The
`ledger_accept` record is the only committed ledger operation associated with
the positive case. No transaction or ledger operation is recorded at a time
between `845357124` and `845357133`; those values are different header-time
anchors, not endpoints of observed elapsed activity.

## Frozen expectation origin

At `scripts/run_checkpoint_1e.py:930`, the runner passed
`int((lookup or {}).get("date", -1))` as `expected_start_date`. The committed
lookup date was `845357124`: the final close time of validated ledger 77. It
was not the preparation time, phase-entry time, submission time, autofill time,
parent close time, or applying-view header time.

At `scripts/e6_oracle.py:386-388`, the oracle computed the expected
NextPaymentDueDate as `expected_start_date + payment_interval`, so
`845357184 = 845357124 + 60`. It had no independent time anchor.

## Pinned 3.4.0 source semantics

Pinned source commit is
`4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`.

- A/B/C: `LoanSet.cpp:225-229` reads `view.header().closeTime` in integer
  seconds. `LoanSet.cpp:430-434,649-666` does so during `LoanSet::doApply`,
  immediately before creating and populating the Loan. This is the applying
  view's in-construction ledger header time—not `parentCloseTime`, a transaction
  field, or the later finalized validated-ledger close time.
- D/E: LoanSet does not supply StartDate and the client cannot choose it. The
  transaction format at `transactions.macro:963-986` has no StartDate field;
  `LoanSet.cpp:650-666` computes and stores it.
- F/G: `LoanSet.cpp:690` initializes NextPaymentDueDate as
  `StartDate + PaymentInterval`; the first interval therefore begins at
  StartDate. `LoanLifecycle_test.cpp:248-273` and
  `LoanTestBase.h:1551-1555` assert the same relationship.
- H: GracePeriod is stored separately (`LoanSet.cpp:683`) and is not added to
  the initial NextPaymentDueDate. The creation overflow bound treats the final
  grace end as `StartDate + PaymentInterval * PaymentTotal + GracePeriod`
  (`LoanSet.cpp:238-278`). With `fixCleanup3_4_0`, default uses an exclusive
  expiry check at `NextPaymentDueDate + GracePeriod`
  (`LoanManage.cpp:106-114`).

The exact standalone construction sequence explains why applying time and
accepted time diverged. `Ledger.cpp:253-278` gives a non-genesis successor a
provisional closeTime of previous closeTime plus its close-time resolution.
`BuildLedger.cpp:38-62` applies transactions against that header. Only after
application does `BuildLedger.cpp:75-80` call `Ledger::setAccepted`, which
replaces the header closeTime with the supplied final close time at
`Ledger.cpp:331-343`. `NetworkOPs.cpp:4520-4534` confirms that
`ledger_accept` drives this standalone consensus path.

Here the committed values establish a 10-second construction increment:
`845357133 - 845357123 = 10`; 10 seconds is a permitted source-defined
resolution (`LedgerTiming.h:17-33`). The artifacts did not independently
capture a `close_time_resolution` field, so that resolution is reported as a
source-and-state reconstruction, not as a separately recorded RPC field.

## Source comparison and nine-second offset

Observed StartDate `845357133` is **SOURCE_CONFORMANT**: it is the value that
pinned LoanSet necessarily copied from the applying view header. Observed
NextPaymentDueDate `845357193` is also **SOURCE_CONFORMANT** because
`845357133 + 60 = 845357193`.

The exact `+9` compares two concrete values attached to different stages:

1. `845357133` — ledger 77's provisional applying-view header closeTime,
   persisted into Loan.StartDate before acceptance; and
2. `845357124` — ledger 77's final accepted close time and transaction date,
   installed after transaction application.

The frozen oracle was nine seconds early because it used the second value to
predict a field defined from the first. This is **A. ORACLE_DEFECT**, not a
protocol contradiction or design ambiguity. The classification does not
rewrite the historical frozen E6 verdict, which remains **FAIL**.

## Schedule, economics, and architecture

The economically requested schedule was preserved: `845357193 - 845357133 =
60`. GracePeriod is 60. For this one-payment schedule, the source-derived grace
boundary is `845357253`, but no lifecycle transaction exercised it.

No client-supplied economic term differs from the validated Loan: principal
1000 integer micro-tRLUSD, one payment, 60-second interval, 60-second grace,
and every requested fee/rate at zero all passed the corrected semantic oracle.
StartDate and NextPaymentDueDate are protocol-derived schedule fields, not
borrower-chosen economic terms.

Separately, the existing positive evidence continues to classify the Credence
authorization composition as **SUPPORTED**: ResearchBot + Credence policy
2-of-2 borrower authorization and the Loan Broker owner's
CounterpartySignature coexisted on the same validated `tesSUCCESS` LoanSet.
That result is not conflated with the timing-oracle defect.

## Future oracle and N1-N6 disposition

No normative oracle was edited. A future oracle should not bind StartDate to
the validated transaction date or another pre-submission timestamp. It should
assert StartDate against the protocol-defined applying-view header time when
that value is independently captured or source-reconstructable, always assert
`NextPaymentDueDate == StartDate + PaymentInterval`, and assert GracePeriod and
its lifecycle boundary separately when exercised.

The existing positive evidence should be retained. A new positive LoanSet or
revised positive experiment is not necessary before the negative program.
N1-N6 may proceed only under a separately authorized checkpoint. None was
executed here.

## Evidence, immutability, and gates

The bounded adjudication evidence is under
`evidence/checkpoint-1e-t/`. Its manifest verifies. Historical directories
`checkpoint-1e-live-01/` and `checkpoint-1e-r1-corrected/` were not modified;
their manifest hashes remain respectively
`42129b0aad5fb7b6f9e641a600ad848f9d8ef47cae26d7beb3fe65559a81d9c8`
and `af51faa819d13b76646f8291d04eee43460311b77686ce00cbe1bc3177433be5`.

- Existing E6 tests: **PASS** (40).
- All spike tests: **PASS** (142).
- Repository unit tests: **PASS** (17); conformance tests: **PASS** (4).
- Lint, formatting, large-file check, strict typecheck, secrets scan, JSON
  validation, all manifests, historical-evidence immutability,
  authorized-path check, and `git diff --check`: **PASS**.
- XRPL transactions: **ZERO**. xrpld starts, RPC calls, and ledger advances:
  **ZERO**.

## Result state and modified paths

- Historical E6: **FAIL**.
- Positive authorization composition: **SUPPORTED**.
- Timing behavior: **SOURCE_CONFORMANT**.
- Timing oracle: **ORACLE_DEFECT**.
- N1-N6: **NOT EXECUTED**.
- E7-E9: **NOT ATTEMPTED**.

Modified paths are limited to:

- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`
- `spike/xrpl/evidence/checkpoint-1e-t/`

---

# Checkpoint 1E-R1-CORRECTED — E6 terms projection repair and adjudication

## Outcome

**HARNESS REPAIR PASS; COMMITTED POSITIVE EVIDENCE CONTRADICTS THE FROZEN
TIMING ORACLE; E6 FAIL.**

This entirely offline checkpoint began at clean commit
`c0ff25cd6408e051206bb893b0b9d28ae3803a40`, with checkpoint-0 ancestry and
submodule gitlinks unchanged. It started no XRPL environment, made no RPC
call, advanced no ledger, and submitted zero transactions.

## Authoritative first-live history and root cause

The committed first-live evidence remains authoritative. The finalized
LoanSet and signing body both contained borrower Account
`rDSspc5iJQBQ1RnwirMw2RPZMxTN7RZvrs`. Transaction
`9D83105152990F291C2052431FC777717CE335A3BCC0CF9D6D2902EBBBA5BF93`
was submitted and validated `tesSUCCESS` in ledger 77. Its pre-state,
post-state, transaction blob, metadata, and public authorization evidence are
unchanged.

The `KeyError: 'Account'` was reproduced offline through the actual integration
shape. The old runner's `terms_from()` projected `LOAN_TERM_FIELDS`, then added
`VaultID`. `LOAN_TERM_FIELDS` excludes `Account`, while
`positive_post_state_assertions()` reads `terms["Account"]` for the Loan
borrower assertion. The incomplete structure was the runner-to-oracle terms
dictionary—not the finalized envelope, signing body, or public evidence.

D6-D9 missed this because their synthetic fixture manually supplied Account.
The original D12 checked only that `exact_loan_set_body` was nonempty and did
not inspect the runner-to-oracle projection.

Pinned rippled 3.4.0 source commit
`4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17` reads `tx[sfAccount]` in
`LoanSet.cpp:282`; when Counterparty equals the Loan Broker owner,
`LoanSet.cpp:303` assigns that Account as the borrower.

## Narrow repair and D12 hardening

The repair separates the signing-evidence term projection from a typed,
validated positive-oracle projection. The latter contains exactly Account,
VaultID, and every `LOAN_TERM_FIELDS` member consumed by the frozen oracle. Its
preflight rejects any absent or `None` field and rejects an Account that differs
from the intended borrower.

The positive oracle itself, finalized transaction, roles, economic terms,
SMT-NUL/CPT-NUL constructions, signatures, expected engine results, state
requirements, and N1-N6 semantics were not changed.

Hardened D12 now requires the actual `positive_oracle_terms` binding, validates
its complete consumed-field set and intended borrower, and requires it to equal
the projection derived from the exact finalized envelope plus the bound Vault
ID. It rejects the historical incomplete projection and accepts the corrected
one. D1-D11 were not weakened.

## Regression results

R1-R12 all pass (28 pytest cases because R8 checks removal of every required
field). R1 reproduces the historical KeyError; R2-R4 prove the committed and
corrected Account flow; R5-R8 enforce projection completeness and identity;
R9 preserves the exact economics; R10 revalidates both authorization domains;
and R11-R12 exercise hardened D12. D1-D12 also pass unchanged in intent.

## Offline positive adjudication

With the corrected projection, the frozen positive oracle completed all
assertions against committed evidence. The result is
**POSITIVE_CONTRADICTED**:

| Assertion | Expected from committed validated transaction date | Observed Loan |
| --- | ---: | ---: |
| `Loan StartDate` | `845357124` | `845357133` |
| `Loan NextPaymentDueDate` | `845357184` | `845357193` |

All other frozen positive semantic assertions passed. Pinned source states
that Loan StartDate uses the applying view header `closeTime`; the committed
runner supplied the validated transaction date as the external expected value.
No reinterpretation or repair of that frozen timing expectation was performed.
Under the checkpoint rule, the positive contradiction sets **E6 = FAIL**.

## Offline authorization adjudication

The committed authorization evidence is **SUPPORTED**. It independently
revalidates ResearchBot and Credence policy borrower signatures, empty
top-level `SigningPubKey`, absence of borrower master-key authorization,
SMT-NUL borrower payloads, the Loan Broker owner's CPT-NUL
`CounterpartySignature`, absence of the Loan Broker pseudo-account from both
signing roles, distinct role prefixes, valid public signatures, and the same
serialized finalized body in both domains.

## Result state, evidence, and recommendation

- E1/E2: **PASS/FROZEN**.
- E3/E4: **PASS/RETIRED within tested scope**.
- E5: **PASS**.
- E6 positive authorization composition: **SUPPORTED**.
- E6 positive frozen semantic oracle: **CONTRADICTED**.
- E6 overall: **FAIL**.
- N1-N6: **NOT EXECUTED in this checkpoint**.
- E7-E9: **NOT ATTEMPTED**.

The new offline evidence manifest covers 5 files and 19768 bytes. The
historical first-live manifest hash remained
`42129b0aad5fb7b6f9e641a600ad848f9d8ef47cae26d7beb3fe65559a81d9c8`
before and after adjudication, and all historical manifests verify.

A new checkpoint must **not** execute only the remaining E6 negatives yet.
The StartDate/validated-transaction-date contradiction requires explicit
review and disposition first. No E7/E8/E9 work or LoanPay is authorized.

## Gates

- R1-R12: **PASS** (28 pytest cases).
- D1-D12: **PASS** (12).
- All spike tests: **PASS** (142).
- Repository unit tests: **PASS** (17).
- Conformance tests: **PASS** (4).
- Lint, formatting, large-file check, strict typecheck, secrets scan, JSON
  validation, all evidence manifests, historical-evidence immutability,
  authorized-path check, and `git diff --check`: **PASS**.

## Checkpoint 1E-R1-CORRECTED modified paths

- `spike/xrpl/scripts/e6_oracle.py`
- `spike/xrpl/scripts/run_checkpoint_1e.py`
- `spike/xrpl/scripts/adjudicate_checkpoint_1e_r1.py`
- `spike/xrpl/tests/test_checkpoint_1e.py`
- `spike/xrpl/tests/test_checkpoint_1e_r1.py`
- `spike/xrpl/evidence/checkpoint-1e-r1-corrected/`
- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`

---

# Checkpoint 1E-LIVE-01 — frozen E6 live execution

## Outcome

**STOP / BLOCKED — the positive LoanSet validated `tesSUCCESS`, but the frozen
runner crashed while constructing the semantic post-state assertions. E6 is
not classified PASS.**

The run began exactly at clean commit
`b4498977d0e17f711bac2705cdbaf7c27db1d97f`, descended from checkpoint-0,
with unchanged submodule gitlinks. D1-D12 passed 12/12 before the fresh ledger
started. The runner was invoked once. It was not changed or retried after live
execution began.

## Frozen provenance and D12

| Artifact | SHA-256 |
| --- | --- |
| `E6-ASSERTIONS.md` | `b7a0b86e250bd30e1bd2968d706aa12bc9a4325ec0127fb2bcb6a06305f3def8` |
| `scripts/e6_oracle.py` | `a97fcc75bb82007bc5f467cb172f93a87cc91d7747f6218f1c31629f2d75acb6` |
| `scripts/run_checkpoint_1e.py` | `bd9607d7258c96229c483cc57e45c2ff3aec81a50287e9a936ede813a8e20b42` |
| `tests/test_checkpoint_1e.py` | `8da5f71f63298b8b13e9ccb06f4ccb783fc00768209a1e3985ea4f2c57e98bd5` |
| xrpl-py binary codec entry point | `7f85b5979191587b70c706e5fab84d2ab1b43f338f5c9563920249c0bb6ae209` |
| xrpl-py keypair implementation | `cbbf8a4720afe6bb2ea4f70ce5abc9383e53e38d64a170f01a55a17a51b080cd` |
| xrpl-py transaction implementation | `00d99ebf6eba59f0e32c830da4faee9d18768ab9df7fb08c80b8a7bdcf532f33` |

The runtime was pinned `xrpld 3.4.0`, source commit
`4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`, build branch `HEAD`, in image
digest `sha256:8cb9cb9e02b5f92d1c8c8721b439e7568acf5da657e54b3f8bb5b8daad32dca9`.
It used the checked-in standalone configuration on network ID 21337 and a
newly recreated data volume. The `feature` and validated Amendments ledger
representations agreed on all eight required amendments.

D12 then passed against the actual runner hash, fresh actors, exact Vault and
Loan Broker IDs, pre-LoanSequence 1, expected Loan index, evidence destination,
and the complete autofilled envelope. The exact body fixed Fee 40, Sequence 7,
LastLedgerSequence 96, NetworkID 21337, empty top-level `SigningPubKey`, and
the frozen LoanSet terms before either authorization domain signed.

## Fresh prerequisite state and finalized terms

The run recreated a public tRLUSD issuer/state, borrower, ResearchBot signer,
Credence policy signer, Vault/Loan Broker owner, cash-basis closed-ended Vault,
100000 micro-tRLUSD Vault deposit, Loan Broker, 10000 micro-tRLUSD cover, and
the borrower quorum-2 SignerList. The Vault entered Investment before LoanSet.
This was prerequisite setup and did not reopen E1-E5.

The submitted body requested 1000 integer micro-tRLUSD, one 60-second payment,
a 60-second grace period, and zero for every frozen fee and rate. It named
Loan Broker `7BD631D3…8FE3`, its owner as Counterparty, and expected Loan index
`CA546361…6ACB`.

## Authorization-domain observation

The validated transaction used the borrower ResearchBot + Credence policy
2-of-2 SignerList with empty top-level `SigningPubKey` and no top-level
`TxnSignature`. Both borrower contributions used `SMT-NUL` / `0x534D5400`
and their respective AccountID suffixes. The borrower master key was not used.

The Loan Broker owner supplied the single `CounterpartySignature` using
`CPT-NUL` / `0x43505400`. Public keys, signatures, payload digests, suffixes,
the common serialized body, final signed blob, and transaction hash are in
`positive-observation.json`. The two role prefixes are distinct and both
domains bind the same serialized finalized body. N5 was not reached, so this
run did not observe altered terms invalidating broker authorization.

## Positive observation and STOP

Transaction `9D83105152990F291C2052431FC777717CE335A3BCC0CF9D6D2902EBBBA5BF93`
validated `tesSUCCESS` in ledger 77. A read-only recovery after STOP observed:

- the expected Loan at `CA546361…6ACB`, borrower and Loan Broker identities,
  principal 1000, one remaining payment, interval/grace 60, and periodic and
  total outstanding values 1000;
- borrower tRLUSD `+1000`, Sequence `+1`, OwnerCount `+1`, and unchanged
  2-of-2 SignerList;
- Vault `AssetsAvailable -1000`, `AssetsTotal` unchanged, and pseudo-account
  tRLUSD `-1000`; and
- Loan Broker `DebtTotal +1000`, LoanSequence `+1`, OwnerCount `+1`, and
  CoverAvailable unchanged.

These are retained observations, not a completed positive oracle verdict. The
frozen runner called `positive_post_state_assertions` with `LOAN_TERM_FIELDS`
plus `VaultID`; that mapping omitted `Account`, which the oracle accesses at
`e6_oracle.py:305`. Python raised `KeyError: 'Account'` before the assertion
list or classification record could be produced. This is a frozen
runner/oracle integration defect. Per the no-repair/no-retry rule, execution
stopped immediately.

## Case results

| Case | Result |
| --- | --- |
| Positive | validated `tesSUCCESS`; semantic classification **BLOCKED** by frozen harness defect |
| N1 | NOT ATTEMPTED |
| N2 | NOT ATTEMPTED |
| N3 | NOT ATTEMPTED |
| N4 | NOT ATTEMPTED |
| N5 | NOT ATTEMPTED |
| N6 | NOT ATTEMPTED, not deferred by protocol preconditions |

No `LoanPay` was submitted. E7-E9 were not executed.

## Evidence, gates, and result state

The evidence manifest covers 30 files and 310535 bytes and verifies with
SHA-256. Historical evidence and the frozen 1E-D artifacts are unchanged.

- D1-D12: **PASS** (12/12 before live start).
- All spike tests: **PASS** (114).
- Repository unit tests: **PASS** (17).
- Conformance tests: **PASS** (4).
- Lint, formatting, large-file check, and strict typecheck: **PASS**.
- Secrets scan: **PASS**.
- JSON validation, all evidence manifests, frozen provenance, historical
  evidence immutability, authorized-path check, and `git diff --check`:
  **PASS**.

Result state: E1/E2 remain **PASS/FROZEN**; E3/E4 remain **PASS/RETIRED within
tested scope**; E5 remains **PASS**; E6 is **BLOCKED**; E7-E9 are
**NOT_ATTEMPTED**.

The Credence XRPL authorization architecture must not yet be treated as
validated because the required positive semantic oracle did not complete and
the bounded negatives did not run. E7/E8/E9 must not begin. The minimum next
work is a separately authorized harness-only correction that includes
`Account` in the oracle terms mapping, plus a regression proving the exact
runner/oracle contract. Any live rerun must be separately authorized and use a
fresh evidence destination. That work was not performed here.

## Checkpoint 1E-LIVE-01 modified paths

- `spike/xrpl/scripts/run_checkpoint_1e.py`
- `spike/xrpl/evidence/checkpoint-1e-live-01/`
- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`

---

# Checkpoint 1E-D — freeze E6 LoanSet composition experiment

## Outcome

**PASS — the E6 authorization-composition experiment and semantic oracles are
source-derived, predeclared, and covered by D1-D12. E6 remains NOT_ATTEMPTED.**

Checkpoint 1E-D began from clean commit
`9642530326cb38aabef3800d37fe3eeaf27dc2fc`, with checkpoint-0 ancestry and
submodule gitlinks unchanged. It inspected the committed E5 authorization
evidence, the E3/E4 Vault and Loan Broker evidence, and rippled 3.4.0 source
commit `4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`.

This checkpoint started no XRPL environment and submitted **zero XRPL
transactions**. It did not create a Vault, Loan Broker, or SignerList and did
not submit `LoanSet` or `LoanPay`.

## Exact parties and authorization domains

The frozen E6 form makes `LoanSet.Account` the borrower and explicitly sets
`LoanSet.Counterparty` to `LoanBroker.Owner`. The Loan Broker ledger object and
its pseudo-account have no signing key; the owner is the protocol counterparty
whose signature is checked independently. With that field arrangement, pinned
`LoanSet::preclaim` derives the borrower as `Account`.

The borrower transaction domain uses the proven ResearchBot + Credence policy
2-of-2 SignerList. Each signer signs `SMT-NUL` / `0x534D5400`, the canonical
transaction signing-field serialization, and its signer AccountID suffix. The
borrower master key is not used.

The Loan Broker-owner domain uses a single signature inside
`CounterpartySignature`. Under `fixCleanup3_4_0` it signs `CPT-NUL` /
`0x43505400` plus the same canonical serialized transaction body.
`CounterpartySignature`, the borrower `Signers` array, top-level
`TxnSignature`, and `SponsorSignature` are excluded signing fields. The two
containers can therefore coexist without changing each other's payload while
the distinct prefixes prevent signature reuse between roles.

The installed serialization implementation can reproduce both public payloads.
The E6 oracle retains each prefix, full payload, SHA-512Half digest, serialized
body, public identity/key/signature, signed blob, and transaction hash. D11
proves synthetically that both domains have distinct prefixes and the identical
body portion. Synthetic signing is not runtime protocol evidence.

## Exact counterparty-bound body

All envelope fields must be frozen before either domain signs: transaction
type, borrower Account, Sequence, Fee, empty top-level `SigningPubKey`,
LastLedgerSequence, NetworkID, LoanBrokerID, explicit Counterparty, and all
present terms. `Data`, sponsorship, delegation, tickets, and LoanSet flags are
omitted.

The exact economic body uses 1000 integer micro-tRLUSD principal, zero
origination/service/late/close/overpayment fees, zero interest/late/close/
overpayment rates, one payment, a 60-second interval, and a 60-second grace
period. N5 changes only `PrincipalRequested` from `1000` in broker-authorized
body X to `1001` in submitted body Y. Pinned source makes this required field
part of the signed body and uses it for disbursement, Loan principal, Vault
assets, and Loan Broker debt.

## fixCleanup3_4_0 conclusion

Before the fix, role signatures shared the ordinary transaction prefixes and
could be copied between roles. With the enabled fix, transaction,
counterparty, and sponsor roles have distinct prefixes; pinned tests reject
role-copying. This is the separation E6 requires. Source review found no
requirement for borrower master-key signing and no conflict between borrower
multisigning and broker-owner counterparty authorization.

## Positive semantic oracle

The frozen positive requires a fresh closed-ended cash-basis Vault in
Investment, funded tRLUSD, a correctly owned/configured Loan Broker with cover,
and the E5 borrower SignerList. Validated `tesSUCCESS` is necessary but
insufficient.

The expected Loan identity is SHA-512Half of namespace `L`, LoanBrokerID, and
the pre-transaction LoanSequence. The exact object must be newly present at
that identity with the borrower, broker, and all frozen terms. Its start and
payment dates, principal, one-payment schedule, zero-fee/rate fields, periodic
payment, total outstanding value, and management fee are asserted.

For the fresh `LEVersion = 1` cash-basis Vault, the oracle requires:

- borrower tRLUSD `+1000`, Vault pseudo-account tRLUSD `-1000`, broker-owner
  tRLUSD unchanged;
- Vault `AssetsAvailable -1000` and `AssetsTotal` unchanged;
- Loan Broker `DebtTotal +1000`, `CoverAvailable` unchanged, LoanSequence `+1`,
  and loan OwnerCount `+1`;
- borrower OwnerCount `+1`, transaction Sequence `+1`, and unchanged semantic
  2-of-2 SignerList; and
- the Loan-to-Broker-to-Vault identity chain exactly matching the frozen IDs.

No assertion uses a global object-count delta.

## Frozen negatives and protected state

| Case | Construction | Expected result |
| --- | --- | --- |
| N1 | valid broker authorization; ResearchBot borrower signer only | `tefBAD_QUORUM` |
| N2 | valid broker authorization; Credence borrower signer only | `tefBAD_QUORUM` |
| N3 | valid borrower 2-of-2; counterparty authorization absent | `temBAD_SIGNER` |
| N4 | valid borrower 2-of-2; invalid broker signature | local `invalidTransaction` with counterparty invalid-signature reason |
| N5 | broker signs X; principal alone changes in Y; borrower signs Y | local `invalidTransaction` with counterparty invalid-signature reason |
| N6 | otherwise valid dual authorization during Subscription | validated `tecTOO_SOON` |

N6 is cleanly orthogonal: the frozen one-payment schedule fits the minimum
Investment interval, while pinned source checks phase before applying LoanSet.

For every negative, the expected Loan identity remains absent and the Vault,
Loan Broker, debt, cover, loan sequence, tRLUSD holdings, borrower SignerList,
and relevant directory relationships remain unchanged. N1-N5 also preserve
the complete borrower AccountRoot. N6 may consume its XRP fee and sequence as
a validated `tec` result; those changes must match exactly and are excluded
from the unchanged lending-state projection.

## Result handling and live gate

Submission, client/local-validation result, validated lookup, and transport
failure remain distinct. Positives require validated semantic state; negatives
require the exact failure plus protected state. A later lookup cannot erase an
earlier authoritative result.

The D12 harness gate raises unless every live binding is populated before a
positive submission path proceeds,
including git commit, assertion hash, runner hash, test hash, build/source,
version, network, amendments, evidence destination, actors, exact body, Vault
ID, Loan Broker ID, pre-loan sequence, and expected Loan index. The eventual
live runner must be hashed after its envelope-building behavior is frozen and
before any live E6 transaction.

## D1-D12

All 12 synthetic/source-derived design tests pass:

- D1/D2 detect either missing borrower signer;
- D3/D4 detect missing or cryptographically invalid broker authorization;
- D5 proves the principal mutation invalidates broker authorization;
- D6 rejects `tesSUCCESS` without the Loan/post-state;
- D7-D9 detect wrong borrower, Vault, or broker relationships;
- D10 detects any protected negative-state mutation;
- D11 captures and separates both public authorization domains; and
- D12 blocks execution with any missing frozen binding.

The design artifacts have these SHA-256 values:

| Artifact | SHA-256 |
| --- | --- |
| `E6-ASSERTIONS.md` | `b7a0b86e250bd30e1bd2968d706aa12bc9a4325ec0127fb2bcb6a06305f3def8` |
| `scripts/e6_oracle.py` | `a97fcc75bb82007bc5f467cb172f93a87cc91d7747f6218f1c31629f2d75acb6` |
| `tests/test_checkpoint_1e.py` | `8da5f71f63298b8b13e9ccb06f4ccb783fc00768209a1e3985ea4f2c57e98bd5` |

## Remaining limits

There is no unresolved source-level protocol ambiguity or architectural
contradiction. There is also no live E6 engine or post-state observation: every
E6 conclusion in this checkpoint is explicitly source-derived or synthetic.
The live envelope values and live runner hash remain runtime bindings and must
be frozen and recorded before a separately authorized execution.

## Gates

- D1-D12: **PASS** (12 tests).
- All spike tests: **PASS** (114 tests).
- Repository unit tests: **PASS** (17 tests).
- Conformance tests: **PASS** (4 tests).
- Lint, formatting, and large-file check: **PASS**.
- Strict typecheck: **PASS** (25 source files).
- Secrets scan: **PASS**.
- JSON validation: **PASS** (139 files).
- Evidence-manifest verification: **PASS** (11 manifests, 120 payloads).
- Historical evidence immutability and authorized-path check: **PASS**.
- `git diff --check`: **PASS**.

## Result state and recommendation

- E1/E2: **PASS/FROZEN**.
- E3/E4: **PASS/RETIRED within tested scope**.
- E5: **PASS**.
- E6-E9: **NOT ATTEMPTED**.
- Checkpoint 1E-D design: **PASS**.

One separately authorized, frozen live E6 run may begin, provided D12 first
passes with the live runner and complete envelope bound. This is not authority
to execute E6. `LoanPay` remains prohibited.

## Checkpoint 1E-D modified paths

- `spike/xrpl/E6-ASSERTIONS.md`
- `spike/xrpl/scripts/e6_oracle.py`
- `spike/xrpl/tests/test_checkpoint_1e.py`
- `spike/xrpl/evidence/checkpoint-1e-d/`
- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`

---

# Checkpoint 1D-LIVE-01 — execute E5 borrower authorization

## Outcome

**PASS — E5 borrower authorization completed against the unchanged frozen
harness.**

Checkpoint 1D-LIVE-01 began from clean commit
`1fd2351e25a7145de84e4689cc0656bd0dcde883`, with checkpoint-0 ancestry and
submodule gitlinks unchanged. All 38 P1-P10 and existing E5 harness tests passed
before the live run. The parameterized runner was invoked exactly once, wrote
only to `evidence/checkpoint-1d-live-01/`, and completed without STOP or retry.

## Frozen provenance

| Component | SHA-256 |
| --- | --- |
| `E5-ASSERTIONS.md` | `656921cacd310214b6ecce7088e21e84fcca44feaa3104b95cfb9f5f4bc92b92` |
| `scripts/e5_oracle.py` | `8052459639081eca2baeb64402120ce00507f35eafcc595315f912016ae07bbf` |
| `scripts/run_checkpoint_1d.py` | `5fc315e0c646dc766ccae349d16b56039e9d54bc41925de2e2d94d85439d12c8` |
| `tests/test_checkpoint_1d.py` | `4a8eba0b49f8a30c50cc5eddfed7a7a8bdc51a29c5c4b241780738d6e0acc7d0` |
| `tests/test_checkpoint_1d_destination.py` | `03d80614ff4f1ba201b1ce2a7db8fb50ced847f909b4427067e5ef925ab71c1a` |

All hashes are embedded in the live provenance and remained byte-identical
after execution.

## Fresh runtime and SignerList setup

The proven mechanism recreated the dedicated volume and started pinned rippled
3.4.0, source commit `4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`,
on network ID `21337`. The fresh validated ledger began at index 2 / close time
`845348010`. The feature and validated Amendments representations agreed on all
eight required amendments.

Four setup funding Payments succeeded. Borrower `SignerListSet` transaction
`674B84F0E51C216C55C0FB5E9FE989EE48942325CE76F647B608B19D9A38FFEB`
returned and validated `tesSUCCESS` at ledger 7 / close time `845348024`.
The repaired API-v1 parser observed exactly one list:

- `SignerQuorum = 2`;
- ResearchBot `rLYaNUeEtpXexaf8Lm5W5gAh7tNcjccDAr`, weight 1;
- Credence policy signer `r4hEaHn2JPyGkmuyf2S4qHxe9295SiLG3N`, weight 1.

No `RegularKey` was present, and the master-key-disabled flag was clear.

## E5 results

| Step | Authorization | Authoritative result | State result | Verdict |
| --- | --- | --- | --- | --- |
| E5-A | ResearchBot only | `tefBAD_QUORUM` | recipient/borrower unchanged | PASS |
| E5-B | Credence only | `tefBAD_QUORUM` | recipient/borrower unchanged | PASS |
| E5-C | ResearchBot + Credence multisign | validated `tesSUCCESS` | recipient +1000; borrower -1030; sequence +1 | PASS |
| E5-D | borrower master key only | validated `tesSUCCESS` | recipient +2000; borrower -2010; sequence +1 | PASS |
| E5-E | final AccountRoot read | no transaction | final assertions passed | PASS |

E5-A transaction
`6E775571BDB287CACAD6D9C6736AAED13AB4ADFEF18EB825FE9BDB80D64A6597`
was not included in the validated ledger. Its evidence contains only the
ResearchBot signer, empty top-level `SigningPubKey`, no top-level signature,
and unchanged protected state.

E5-B transaction
`F41B1CEB422F1FB51856EAF73A4B5BA9E7DB741F7503B8F48BE38A9F4753A599`
was not included in the validated ledger. Its evidence contains only the
Credence policy signer and the same protected-state guarantees.

E5-C transaction
`79486BFBB25376825E9567300FEB6D539D0BF84BB5824BC30A784C42E9F640A1`
validated at ledger 10. Both signer identities, public keys, and signatures are
present; the top-level signing key is empty and no master-key signature is
present.

E5-D transaction
`AAFFA8627561E65E5A366F158B8363F238A708759F8DA6A81C4CE717C543434D`
validated at ledger 11. It contains the borrower's public master key and
top-level signature and has no `Signers` array.

E5-E passed with AccountRoot flags `0` and the exact conclusion: **No
RegularKey is configured in the tested borrower state.** The
master-key-disabled flag remains clear, and the 2-of-2 SignerList remains
unchanged.

## Public evidence, manifest, and scope

The live artifacts retain actor account IDs and public keys, signing mode,
`SigningPubKey`, `Signers`, public transaction signatures, signed blobs,
transaction hashes, submission results, validated lookups where applicable,
ledger indices, and protected before/after state. The secrets scan found no
seed or private-key material.

The runner submitted nine transactions: four setup funding Payments, one
`SignerListSet`, and E5-A through E5-D. It made nine scripted closes. E5-E was a
read-only final assertion. No vault, Loan Broker, `LoanSet`, `LoanPay`, or other
lending transaction was submitted.

`evidence/checkpoint-1d-live-01/MANIFEST.json` verifies 19 payload files totaling
339157 bytes.

## Result state

- E1/E2: **PASS/FROZEN**.
- E3/E4: **PASS/RETIRED within tested scope**.
- E5: **PASS**.
- E6-E9: **NOT ATTEMPTED**.

## Gates

- P1-P10: **PASS** (10 tests).
- S1-S12 and all existing E5 tests: **PASS** (28 tests).
- All spike tests: **PASS** (102 tests).
- Repository unit tests: **PASS** (17 tests).
- Conformance tests: **PASS** (4 tests).
- Lint, formatting, and large-file check: **PASS**.
- Strict typecheck: **PASS** (25 source files).
- Secrets scan: **PASS**.
- JSON validation: **PASS** (136 files).
- Evidence-manifest verification: **PASS** (10 manifests, 118 payload files).
- Historical evidence immutability: **PASS**.
- Frozen provenance immutability: **PASS**.
- Authorized-path and unchanged review/submodule checks: **PASS**.
- `git diff --check`: **PASS**.

## Recommendation

E5's borrower-authorization gate is complete. E6 may begin only under a
separate explicit authorization; it was not executed here.

## Checkpoint 1D-LIVE-01 modified paths

- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`
- `spike/xrpl/evidence/checkpoint-1d-live-01/`

---

# Checkpoint 1D-P — E5 evidence destination parameterization

## Outcome

**PASS — evidence-destination plumbing is explicit and fail-closed. E5 remains
BLOCKED.**

Checkpoint 1D-P began from clean commit
`c522d2d2bad9b1d93bdd4c5d2244074b4042232e`, with checkpoint-0 ancestry and
submodule gitlinks unchanged. No rippled environment was started, no XRPL RPC
was called, no ledger was advanced, and **zero XRPL transactions were
submitted**.

## Root cause and repair

The prior runner hard-coded `evidence/checkpoint-1d/`, so it could neither
preserve that historical run nor create the required separate execution
record. The runner now requires an explicit `--evidence-dir` argument and
passes the resolved destination through the runner, provenance, result
metadata, per-transaction records, STOP artifacts, and manifest generation.

The resolver accepts only destinations below `spike/xrpl/evidence/`, rejects
`..` traversal and resolved path escape, and refuses the evidence root itself.
The preparation step accepts a new or empty selected directory and refuses any
non-empty destination. There is no default checkpoint name and no overwrite
mode.

## P1-P10

| Test | Result |
| --- | --- |
| P1 explicit `checkpoint-1d-x` destination in an isolated root | PASS |
| P2 runner writes to the selected destination, not `checkpoint-1d` | PASS |
| P3 historical `checkpoint-1d` tree remains byte-identical | PASS |
| P4 missing explicit destination | PASS — rejected |
| P5 destination outside evidence root | PASS — rejected |
| P6 path traversal | PASS — rejected |
| P7 non-empty authoritative destination reuse | PASS — rejected |
| P8 manifest generated in selected destination | PASS |
| P9 metadata references selected destination | PASS |
| P10 S1-S12 and semantic files remain byte-identical | PASS |

All S1-S12 tests pass unchanged. The full existing E5 test file is unchanged
and all 28 tests pass.

## Provenance and historical evidence

The future live run will record these current components:

| Component | SHA-256 |
| --- | --- |
| New runner | `5fc315e0c646dc766ccae349d16b56039e9d54bc41925de2e2d94d85439d12c8` |
| Destination tests | `03d80614ff4f1ba201b1ce2a7db8fb50ced847f909b4427067e5ef925ab71c1a` |
| Assertions | `656921cacd310214b6ecce7088e21e84fcca44feaa3104b95cfb9f5f4bc92b92` |
| SignerList parser/oracle | `8052459639081eca2baeb64402120ce00507f35eafcc595315f912016ae07bbf` |
| Existing E5 tests | `4a8eba0b49f8a30c50cc5eddfed7a7a8bdc51a29c5c4b241780738d6e0acc7d0` |

The assertion, parser/oracle, and existing E5 test hashes are unchanged from
1D-H/1D-X. No file under `spike/xrpl/evidence/` was modified, renamed, or
moved.

## Retained result state

- E1/E2: **PASS/FROZEN**.
- E3/E4: **PASS/RETIRED within tested scope**.
- E5: **BLOCKED**; E5-A through E5-E remain **NOT ATTEMPTED**.
- E6-E9: **NOT ATTEMPTED**.

## Gates

- P1-P10: **PASS** (10 tests).
- S1-S12 and all existing E5 tests: **PASS** (28 tests).
- All spike tests: **PASS** (102 tests).
- Repository unit tests: **PASS** (17 tests).
- Conformance tests: **PASS** (4 tests).
- Lint, formatting, and large-file check: **PASS**.
- Strict typecheck: **PASS** (25 source files).
- Secrets scan: **PASS**.
- JSON validation: **PASS** (116 files).
- Historical evidence-manifest verification: **PASS** (9 manifests, 99 payload files).
- Historical evidence immutability: **PASS**.
- Authorized-path and unchanged review/submodule checks: **PASS**.
- `git diff --check`: **PASS**.

## Readiness

The parameterized harness is ready to execute against a separately authorized,
new or empty evidence destination. The current
`evidence/checkpoint-1d-x/` directory contains the authoritative 1D-X STOP
record, so P7 correctly refuses it. Therefore a fresh E5 execution may **not**
begin under the existing exact destination instruction; an unused destination
must first be explicitly authorized. E6 must not begin.

## Checkpoint 1D-P modified paths

- `spike/xrpl/scripts/run_checkpoint_1d.py`
- `spike/xrpl/tests/test_checkpoint_1d_destination.py`
- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`

---

# Checkpoint 1D-X — execute E5 borrower authorization

## Outcome

**BLOCKED — fail-closed during the frozen-harness preflight, before runtime.**

Checkpoint 1D-X began from clean commit
`f4a7dff1c9de14ff20861992c734990bac8866d9`, with checkpoint-0 ancestry and
submodule gitlinks unchanged. The 28 existing E5 harness tests, including
S1-S12, passed before execution.

The frozen runner binds `EVIDENCE` to `evidence/checkpoint-1d/`. That committed
directory already exists, and the runner's retry guard stops whenever it
exists. Checkpoint 1D-X instead requires all new live evidence under
`evidence/checkpoint-1d-x/`. The frozen runner therefore cannot execute this
checkpoint while satisfying both its own guard and the required evidence
destination.

Changing the path, moving or removing committed evidence, or routing the run
through another directory would alter or work around the frozen harness. The
checkpoint requires STOP in that situation, so no runtime was started and the
runner was not invoked.

## Frozen provenance

| Frozen component | SHA-256 |
| --- | --- |
| `E5-ASSERTIONS.md` | `656921cacd310214b6ecce7088e21e84fcca44feaa3104b95cfb9f5f4bc92b92` |
| `scripts/e5_oracle.py` | `8052459639081eca2baeb64402120ce00507f35eafcc595315f912016ae07bbf` |
| `scripts/run_checkpoint_1d.py` | `b158f8364c96cf6b296ad1b70b07c3301f609a6da7a21cd3c1975d1e3f5c0a5b` |
| `tests/test_checkpoint_1d.py` | `4a8eba0b49f8a30c50cc5eddfed7a7a8bdc51a29c5c4b241780738d6e0acc7d0` |

All four hashes remained unchanged after STOP.

## Execution status

- Fresh pinned runtime: **NOT STARTED**.
- Frozen E1 invariant verification: **NOT RUN in 1D-X**.
- SignerListSet: **NOT SUBMITTED**.
- Resulting 1D-X SignerList state: **NOT OBSERVED**.
- E5-A ResearchBot alone: **NOT ATTEMPTED**.
- E5-B Credence alone: **NOT ATTEMPTED**.
- E5-C ResearchBot + Credence: **NOT ATTEMPTED**.
- E5-D borrower master key: **NOT ATTEMPTED**.
- E5-E final RegularKey/master-key-flag read: **NOT ATTEMPTED**.
- Public signer evidence: **NOT PRODUCED** because no E5 transaction was
  constructed or submitted.

There were zero runner executions, zero XRPL RPC calls, zero ledger advances,
and **zero new XRPL transactions**. No `LoanSet`, `LoanPay`, or other lending
transaction was submitted.

## Classification and retained result state

Classification: **A — local frozen-harness evidence-destination mismatch**.
This is not a protocol or authorization result.

- E1/E2: **PASS/FROZEN**.
- E3/E4: **PASS/RETIRED within tested scope**.
- E5: **BLOCKED**; E5-A through E5-E remain **NOT ATTEMPTED**.
- E6-E9: **NOT ATTEMPTED**.

## Evidence and gates

`evidence/checkpoint-1d-x/MANIFEST.json` verifies the single fail-closed payload.

- Pre-live E5 harness tests including S1-S12: **PASS** (28 tests).
- All spike tests: **PASS** (92 tests).
- Repository unit tests: **PASS** (17 tests).
- Conformance tests: **PASS** (4 tests).
- Lint, formatting, and large-file check: **PASS**.
- Strict typecheck: **PASS** (25 source files).
- Secrets scan: **PASS**.
- JSON validation: **PASS** (116 files).
- Evidence-manifest verification: **PASS** (9 manifests, 99 payload files).
- Frozen provenance immutability: **PASS**.
- Authorized-path and unchanged review/submodule checks: **PASS**.
- `git diff --check`: **PASS**.

## Recommendation

**E6 must not begin.** The minimum next checkpoint is a separately authorized,
harness-only evidence-destination repair and regression that binds the frozen
E5 execution to `checkpoint-1d-x/`. Only after that repair is frozen may a
separately authorized fresh E5-only run be considered. Neither action was
performed here.

## Checkpoint 1D-X modified paths

- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`
- `spike/xrpl/evidence/checkpoint-1d-x/`

---

# Checkpoint 1D-H — SignerList response-shape repair

## Outcome

**PASS — the narrow harness repair is complete. E5 remains BLOCKED.**

Checkpoint 1D-H began from clean commit
`2368e5c32f0be5f764edd56b0d8bd3cc6d810810`, with checkpoint-0 ancestry and
submodule gitlinks unchanged. No rippled environment was started, no RPC call
was made, no ledger was advanced, and **zero XRPL transactions were submitted**.

## False-block reproduction and pinned response semantics

The primary S1 fixture reads the committed Checkpoint 1D SignerListSet record.
The legacy top-level `after_state.signer_lists` value is empty and reproduces
the false BLOCK. The same record preserves the actual response content under
`after_state.borrower_account.signer_lists`, corresponding to the original
`result.account_data.signer_lists` path. It contains one list with quorum 2,
ResearchBot weight 1, and Credence policy signer weight 1.

Pinned rippled source commit
`4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17` establishes that unspecified
network RPC versions use API version 1, and that `account_info` places requested
SignerLists under `account_data.signer_lists` for API version 1. The source and
its own API-version-1 tests are linked in `SIGNERLIST-REPAIR.md`.

These remain setup observations. They do not establish E5-A through E5-E.

## Exact repair

The E5 state reader now consumes only the pinned API-version-1 location. The
structural parser rejects missing/non-object `account_data`, missing
`signer_lists`, and a non-array value. The semantic validator then requires
exactly one list, quorum 2, and exactly the unique ResearchBot/Credence weight-1
mapping. It rejects malformed, missing, extra, duplicate, or incorrectly
weighted entries and treats entry ordering as non-semantic. It does not guess
between response paths.

## S1-S12 regression results

| Test | Result |
| --- | --- |
| S1 committed Checkpoint 1D response | PASS |
| S2 missing `account_data` | PASS — rejected |
| S3 missing `signer_lists` | PASS — rejected |
| S4 wrong quorum | PASS — rejected |
| S5 missing ResearchBot | PASS — rejected |
| S6 missing Credence signer | PASS — rejected |
| S7 wrong signer weight | PASS — rejected |
| S8 unexpected extra signer | PASS — rejected |
| S9 reordered entries | PASS — accepted |
| S10 duplicate signer identity | PASS — rejected |
| S11 absent versus configured `RegularKey` | PASS — distinguished |
| S12 clear versus set master-key-disabled flag | PASS — distinguished |

## Signer-evidence and provenance audit

The future E5-A through E5-D paths already retain public signer identities,
top-level `SigningPubKey`, the `Signers` array, public signatures, signed blob,
transaction hash, submission result, validated lookup where applicable, and
before/after protected state. Existing D5-D14 tests exercise those capture and
assertion paths. No required signer-evidence binding is missing.

Future E5 provenance continues to hash the assertions, oracle, runner, and
tests. It now also explicitly binds the SignerList parser to
`scripts/e5_oracle.py` and records that component's SHA-256.

## Retained result state

- E1: **PASS/FROZEN**.
- E2: **PASS/FROZEN**.
- E3: **PASS/RETIRED within tested scope**.
- E4: **PASS/RETIRED within tested scope**.
- E5: **BLOCKED**; E5-A through E5-E remain **NOT ATTEMPTED**.
- E6-E9: **NOT ATTEMPTED**.

## Gates

- S1-S12: **PASS**.
- All Checkpoint 1D tests: **PASS** (28 tests).
- All spike tests: **PASS** (92 tests).
- Repository unit tests: **PASS** (17 tests).
- Conformance tests: **PASS** (4 tests).
- Lint, formatting, and large-file check: **PASS**.
- Strict typecheck: **PASS** (25 source files).
- Secrets scan: **PASS**.
- JSON validation: **PASS** (114 files).
- Evidence-manifest verification: **PASS** (8 manifests, 98 payload files).
- Zero-transaction/static scope audit: **PASS**.
- Authorized-path and unchanged review/submodule checks: **PASS**.
- `git diff --check`: **PASS**.

## Recommendation

The harness is ready for a separately authorized fresh E5-only rerun. E5 has
not been upgraded, and E6 must not begin.

## Checkpoint 1D-H modified paths

- `spike/xrpl/SIGNERLIST-REPAIR.md`
- `spike/xrpl/scripts/e5_oracle.py`
- `spike/xrpl/scripts/run_checkpoint_1d.py`
- `spike/xrpl/tests/test_checkpoint_1d.py`
- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`

---

# Checkpoint 1D — E5 borrower authorization

## Outcome

**BLOCKED — fail-closed during the SignerList setup assertion, before E5-A.**

Checkpoint 1D began from clean commit
`44fb089f0cb4281608ab0b9a67e129147f349887`, with checkpoint-0 ancestry
and submodule gitlinks unchanged. The independent E3/E4 audit commit was
`593eb79694e86238af44785bf8d63e9a3e8086c9`, verdict **PASS WITH
CONDITIONS**.

The live runner executed once. It submitted four setup funding Payments and
one borrower `SignerListSet`. The first frozen SignerList post-state assertion
failed, so no E5-A through E5-E operation was submitted or evaluated. No
`LoanSet`, `LoanPay`, `LoanManage`, or other lending transaction was submitted.

## Run provenance

The runner recorded the start commit and these pre-run hashes inside the live
evidence:

| File | SHA-256 |
| --- | --- |
| `E5-ASSERTIONS.md` | `656921cacd310214b6ecce7088e21e84fcca44feaa3104b95cfb9f5f4bc92b92` |
| `scripts/e5_oracle.py` | `5154948e19e39a350529c79d9acee2234720b6285180feaa489bb930be455e3b` |
| `scripts/run_checkpoint_1d.py` | `1ba520457c1b538c97e71394fb62791d5ea89d88016564a24665a96bf9fcd503` |
| `tests/test_checkpoint_1d.py` | `007ae32bf186093d82e8fce12031809fbd04977cb5adaeed5a9ee5ffd66faee8` |

Those files remain byte-identical after STOP. The 15 synthetic E5 oracle tests
and all 79 spike tests passed before the live run.

## Fresh runtime

The committed mechanism recreated the dedicated volume and started pinned
xrpld 3.4.0, source commit
`4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`, on network ID `21337`.
The fresh ledger began at index 2 / close time `845339350`. Both the `feature`
RPC and the validated Amendments ledger object contained the eight required
amendments, and the frozen cross-check passed.

## SignerList STOP

`SignerListSet` transaction
`A55D6968F643284A7148D16B45E0DBE0F4D7DB0E8E45EBB4EB51D6FF56603D4C`
returned `tesSUCCESS` and validated as `tesSUCCESS` at ledger 7 / close time
`845339355`. The borrower sequence advanced by one, no `RegularKey` was
present, and the master-key-disabled flag was clear.

The frozen harness read `result.signer_lists`, observed an empty list, and
failed its count, quorum, and entry assertions. The same captured validated
response places the installed signer list under
`result.account_data.signer_lists`. That nested object has `SignerQuorum = 2`
and exactly the ResearchBot and Credence policy accounts at weight 1 each.

This is classified **A — local harness response-shape assertion error**. It is
not a protocol contradiction, but the checkpoint rule requires STOP because
the runner/oracle would have to change to continue. No correction or retry was
performed.

## E5 status

- E5-A ResearchBot alone: **NOT ATTEMPTED**.
- E5-B Credence alone: **NOT ATTEMPTED**.
- E5-C ResearchBot + Credence: **NOT ATTEMPTED**.
- E5-D owner master key: **NOT ATTEMPTED**.
- E5-E regular-key absence: **NOT ATTEMPTED as an E5 conclusion**. Setup
  evidence showed no `RegularKey`, but E5 stopped before the predeclared final
  read.

E5 verdict: **BLOCKED**. E1 and E2 remain **PASS/FROZEN**; E3 and E4 remain
**PASS/RETIRED within tested scope**; E6-E9 remain **NOT ATTEMPTED**.

## Evidence

`evidence/checkpoint-1d/MANIFEST.json` verifies 13 payload files totaling
116077 bytes. The evidence includes run provenance, fresh environment and
amendment state, public actor identities and public keys, five setup transaction
records, five ledger closes, the exact unexpected result, and its protected
manifest. The security scan found no seed or private-key material.

## Gates

- E5 synthetic oracle tests: **PASS** (15 tests).
- All spike tests: **PASS** (79 tests).
- Repository unit tests: **PASS** (17 tests).
- Conformance tests: **PASS** (4 tests).
- Lint, formatting, and large-file check: **PASS** (71 formatted files checked).
- Strict typecheck: **PASS** (25 source files).
- Secrets scan: **PASS**.
- JSON validation: **PASS** (114 files).
- Evidence-manifest verification: **PASS** (8 manifests, 98 payload files).
- E5 assertion/oracle/runner/test immutability after live execution: **PASS**.
- Authorized-path check: **PASS**.
- `git diff --check`: **PASS**.

## Recommendation

**E6 must not begin.** The minimum next checkpoint is a separately authorized,
harness-only correction that reads the validated SignerList from the pinned
3.4.0 response shape, with a regression fixture for the captured response.
Only after that correction is frozen may a fresh E5-only rerun be considered.
Neither action was performed here.

The permitted fail-closed artifacts are committed after all required gates
pass. The prescribed success-only commit message is not used because E5 did
not complete.

## Checkpoint 1D modified paths

- `spike/xrpl/E5-ASSERTIONS.md`
- `spike/xrpl/scripts/e5_oracle.py`
- `spike/xrpl/scripts/run_checkpoint_1d.py`
- `spike/xrpl/tests/test_checkpoint_1d.py`
- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`
- `spike/xrpl/evidence/checkpoint-1d/`

---

# Checkpoint 1C-X — execute frozen E3/E4 oracle

## Outcome

**PASS — E3 and E4 completed against the unchanged frozen A-K oracle.**

Checkpoint 1C-X began at clean commit
`5369ff0fe61b0d64faebc95b72d41e6a6938c6ae`, retained checkpoint-0 ancestry
and unchanged submodule gitlinks, and modified only `spike/xrpl/` report/result
and live-evidence artifacts.

The committed runner was executed exactly once. It recorded the required order
`A → B → C → D → E → F → G → H → I → J → K`. No step failed, no retry was
performed, and E5-E9 were not executed.

## Frozen-oracle immutability

The following start hashes are unchanged after the run:

| Frozen file | SHA-256 |
| --- | --- |
| `STATE-ASSERTIONS.md` | `836b90b7db41b04a9fdefb78d340f030f798b7650b4945b89ba48990545b416a` |
| `scripts/harness.py` | `098bc81f3358b8c68a235bf8d9c9101f209ff7074232fb9021f645a4b0efae7b` |
| `scripts/frozen_oracle.py` | `bfa00b417de86518a1c9eab266c6fb138f9b3f677373e58c96a97175c300b2be` |
| `tests/test_state_assertions.py` | `634880c33181da678d8dead2dac1e932c0dd6aafac44c862fa48259b7710b77c` |
| `tests/test_frozen_oracle_runner.py` | `935e53b4c66f99e2332613d94bc00582f331bc3d86cf1f4835147bff9fc3e37e` |

No frozen assertion, binding, expected result, classification rule, or test was
changed during or after execution.

## Fresh runtime and prerequisite setup

The committed reproducible mechanism rebuilt the pinned xrpld 3.4.0 image,
removed and recreated the dedicated volume, and started a fresh standalone
ledger at index 2 and close time `845331860`. The server reported network ID
`21337`; the frozen E1 cross-check accepted all eight required amendments.

The ten prerequisite transactions recreated the four public actors, issuer
DefaultRipple flag, three tRLUSD trust lines, lender balance `200000`, and Loan
Broker owner balance `20000`. This was SETUP only and did not reopen E2. The
fixture reached validated ledger 12.

## A-K live results

| Step | Result | Validation | Ledger transition | Close-time transition |
| --- | --- | --- | --- | --- |
| A VaultCreate | PASS — `tesSUCCESS` | `VALIDATED` / `tesSUCCESS` | 12 → 13 | 845331870 → 845331871 |
| B Subscription deposit | PASS — `tesSUCCESS` | `VALIDATED` / `tesSUCCESS` | 13 → 14 | 845331871 → 845331872 |
| C Subscription withdrawal | PASS — `tesSUCCESS` | `VALIDATED` / `tesSUCCESS` | 14 → 15 | 845331872 → 845331873 |
| D Investment deposit rejection | PASS — `tecEXPIRED` | not included; authoritative submission retained | 43 → 44 | 845331901 → 845331902 |
| E Investment withdrawal rejection | PASS — `tecTOO_SOON` | not included; authoritative submission retained | 44 → 45 | 845331902 → 845331903 |
| F Redemption withdrawal | PASS — `tesSUCCESS` | `VALIDATED` / `tesSUCCESS` | 222 → 223 | 845332080 → 845332081 |
| G Redemption deposit rejection | PASS — `tecEXPIRED` | not included; authoritative submission retained | 223 → 224 | 845332081 → 845332082 |
| H Loan Broker creation | PASS — `tesSUCCESS` | `VALIDATED` / `tesSUCCESS` | 225 → 226 | 845332083 → 845332084 |
| I Vault/Broker relationship | PASS — inherited H evidence | `VALIDATED` / `tesSUCCESS` | inherited 225 → 226 | inherited 845332083 → 845332084 |
| J cover establishment | PASS — `tesSUCCESS` | `VALIDATED` / `tesSUCCESS` | 226 → 227 | 845332084 → 845332085 |
| K non-owner negative | PASS — `tecNO_PERMISSION` | not included; authoritative submission retained | 227 → 228 | 845332085 → 845332086 |

Every negative operation retained the exact authoritative submission result and
passed complete protected-state equality. Every positive operation had matching
validated metadata and passed all bound semantic predicates.

## E3 phase observations

The E3 Vault was created with SubscriptionDate `845331900` and RedemptionDate
`845332080`.

- Subscription operations B and C executed at parent close times `845331871`
  and `845331872`.
- From ledger 15 / close time `845331873`, exactly 28 scripted closes entered
  Investment at ledger 43 / close time `845331901`, one second after the
  SubscriptionDate.
- D and E executed in Investment at parent close times `845331901` and
  `845331902`.
- From ledger 45 / close time `845331903`, exactly 177 scripted closes entered
  Redemption at ledger 222 / close time `845332080`, exactly the configured
  RedemptionDate.
- F and G executed in Redemption at parent close times `845332080` and
  `845332081`.

No wall-clock sleep was used as phase evidence. The complete checkpoint made
226 scripted closes, including setup, transaction validation/absence closes,
and both phase transitions.

E3 verdict: **PASS**. This does not claim LoanSet phase behavior; no LoanSet was
submitted.

The E3 Vault ID is
`B87C0BAC5B3E2E11A63CF26576F777553FCBE36376493F0D407AC485FCE65C1B`.
Its final state recorded `AssetsTotal = 98000`, `AssetsAvailable = 98000`,
lender tRLUSD balance `102000`, and outstanding/holder shares
`98000000000`.

## E4 cover observation

The E4 Vault ID is
`D17C5E01705B3A6BD3268147679415031F5B20757FA804FB46A762CD121508CA`.
The LoanBroker ID is
`220A56298757CBA15A63DDB58685F6F0ACA4902BEC70DEE6541060310ED0AEC8`.
H observed the LoanBroker owner equal to the Vault owner and I observed the
VaultID relationship plus present, distinct Vault and LoanBroker pseudo
accounts.

J observed:

- `CoverAvailable = 10000`;
- LoanBroker pseudo-account tRLUSD holding `0 → 10000`;
- owner tRLUSD balance `20000 → 10000`;
- `CoverRateMinimum = 10000`;
- `CoverRateLiquidation = 25000`;
- `DebtMaximum = 50000`; and
- `DebtTotal = 0`.

K observed `tecNO_PERMISSION` for the non-owner LoanBrokerSet attempt, no
matching non-owner LoanBroker, and unchanged protected state.

E4 verdict: **PASS**.

### Cover conclusions

**OBSERVED:** the exact configured fields and tRLUSD balance/holding deltas
listed above, with zero debt and the expected Vault/Broker ownership
relationship.

**DERIVED:** pinned protocol semantics bound default cover by the applicable
rate, debt/default amount, and `CoverAvailable`, using protocol
tenth-basis-point scaling.

**UNRESOLVED:** default, realized loss, liquidation, clawback, and lender-loss
behavior were not executed. No claim is made that cover guarantees lenders
against all losses.

## Evidence manifest

`evidence/checkpoint-1c-x/MANIFEST.json` verifies 34 public evidence files
totaling 1197099 bytes. The directory contains environment identity, public
actors, prerequisite state, 21 transaction records, 226 closes, all eleven A-K
records, phase transitions, E3/E4 results, and the final checkpoint result.
Seeds, private keys, signatures, signing public keys, and transaction blobs are
absent.

## Gates

- H1-H12: **PASS** (17 tests).
- B1-B12: **PASS** (29 tests).
- All spike tests: **PASS** (64 tests).
- Repository unit tests: **PASS** (17 tests).
- Conformance tests: **PASS** (4 tests).
- Lint, formatting, and large-file check: **PASS** (67 formatted files checked).
- Strict typecheck: **PASS** (25 source files).
- Secrets scan: **PASS**.
- JSON validation: **PASS** (110 files).
- Evidence-manifest verification: **PASS** (7 manifests, 85 payload files).
- Frozen oracle/bindings immutability: **PASS**.
- Checkpoint-0 ancestry and unchanged submodule gitlinks: **PASS**.
- Authorized-path check: **PASS**.
- `git diff --check`: **PASS**.

## Experiment state and recommendation

- E1: **PASS/FROZEN**
- E2: **PASS/FROZEN**
- E3: **PASS**
- E4: **PASS**
- E5-E9: **NOT_ATTEMPTED**

Overall Checkpoint 1C-X verdict: **PASS**. The E3/E4 stop/go protocol gate is
satisfied. Consistent with the adjudication workflow, the next action should be
an independent bounded evidence audit of this commit before authorizing E5.
E5 and E6 must not begin during this checkpoint.

## Checkpoint 1C-X modified paths

- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`
- `spike/xrpl/evidence/checkpoint-1c-x/`

---

# Checkpoint 1C-B — bind frozen A-K oracle to runner

## Outcome

**Harness checkpoint: PASS. Protocol experiments remain frozen.**

Checkpoint 1C-B began at clean commit
`377d994eee74d489f89f22509547a7b2ec857c0a`, retained checkpoint-0 ancestry
and unchanged submodule gitlinks, and modified only `spike/xrpl/`.

**ZERO XRPL transactions were submitted.** xrpld was not started, no RPC call
was made, and no ledger was closed, advanced, or mutated. The complete A-K
control flow ran only against explicitly synthetic fixtures.

## Frozen inputs

| Frozen file | Before SHA-256 | After SHA-256 | Result |
| --- | --- | --- | --- |
| `STATE-ASSERTIONS.md` | `836b90b7db41b04a9fdefb78d340f030f798b7650b4945b89ba48990545b416a` | `836b90b7db41b04a9fdefb78d340f030f798b7650b4945b89ba48990545b416a` | UNCHANGED |
| `tests/test_state_assertions.py` | `634880c33181da678d8dead2dac1e932c0dd6aafac44c862fa48259b7710b77c` | `634880c33181da678d8dead2dac1e932c0dd6aafac44c862fa48259b7710b77c` | UNCHANGED |

The semantic helper file was also left unchanged. No A-K meaning, expected
result, predicate, or order was changed.

## Binding architecture

`scripts/frozen_oracle.py` defines an ordered, data-driven binding registry for
A-K. Each binding declares its frozen result, predicate identifiers, capture
paths, positive validated-state requirement, and negative protected-state
requirement. A static preflight rejects a missing/reordered step, missing
predicate or capture path, positive binding without validated post-state, and
negative binding without protected-state comparison.

`OracleSession` accepts observations only in A-K order, invokes every declared
predicate, serializes the required evidence metadata, and permanently stops on
the first FAIL. Runtime verification rejects an evaluator that declares but
does not invoke a predicate.

The protocol runner now constructs the preflighted session before its first RPC
and maps live captures into the same binding engine. Step I inherits H's
validated transaction evidence and adds no submission. The fixture adapter
exercises the identical registry and evaluation path without importing a key,
starting a server, or making a network call.

## A-K bindings

| Step | Bound frozen behavior |
| --- | --- |
| A | Unique metadata Vault index; semantic Vault and owner MPToken; indexed pre-state absence; protected owner trust line; captured issuance and pseudo-account. |
| B | Subscription phase; stable Vault/share identities; exact deposit asset, balance, issuance, and holder deltas; unrelated-state protection. |
| C | Subscription phase; the same stable identities and protected state; exact inverse withdrawal deltas. |
| D | Investment phase; authoritative `tecEXPIRED`; complete protected-state equality including lender sequence. |
| E | Investment phase; authoritative `tecTOO_SOON`; the same complete protected-state equality. |
| F | Redemption phase; stable identities and exact withdrawal deltas; unrelated-state protection. |
| G | Redemption phase; authoritative `tecEXPIRED`; complete protected-state equality. |
| H | Unique metadata LoanBroker index; semantic owner/Vault/rate/limit/default fields; indexed pre-state absence; protected Vault/owner asset; scoped OwnerCount effect; pseudo-account capture. |
| I | H-derived validated evidence; VaultID/Owner equality; present and distinct Vault/Broker pseudo accounts; stable Vault configuration. |
| J | Stable broker configuration; exact cover/owner/pseudo-holding deltas; zero debt; protected Vault and unrelated state. |
| K | Authoritative `tecNO_PERMISSION`; explicit absence of the attempted non-owner LoanBroker identity; complete owner/non-owner protected-state equality. |

## B1-B12 results

| Test | Result |
| --- | --- |
| B1 complete A-K preflight | PASS |
| B2 missing A rejected | PASS |
| B3 missing K rejected | PASS |
| B4 reordered A-K rejected | PASS |
| B5 missing predicate binding rejected | PASS |
| B6 `tesSUCCESS` without validated post-state rejected | PASS |
| B7 negative result with changed protected state rejected | PASS |
| B8 client exception recorded distinctly | PASS |
| B9 D failure prevents E-K | PASS |
| B10 fixture dry run traverses A-K | PASS |
| B11 every output contains required evidence metadata | PASS |
| B12 H1-H12 file unchanged | PASS |

Additional regressions prove preflight rejection for missing capture,
positive-validation, and negative-protection bindings; runtime rejection when a
declared predicate is not invoked; the live runner's A-K source order; and
short-circuit behavior independently at every A-K step. The 1C-B test module
contains 29 passing cases.

## Synthetic fixture dry run

The dry run traversed exactly
`A → B → C → D → E → F → G → H → I → J → K`. All eleven records passed their
bound assertions and contain the required submission/client/validation,
pre/post state, ledger index, close time, assertion, and classification fields.

`evidence/checkpoint-1c-b/fixture-dry-run.json` is labeled
`SYNTHETIC HARNESS EVIDENCE — NOT XRPL PROTOCOL EVIDENCE`. Its SHA-256 is
`24acbf1cc6de3643beb3d4cd4e4d7d8dc546adb08f016264014f6a5d3f61030f`.
The manifest covers one file totaling 107421 bytes.

The failure fixtures prove a failing step prevents every later provider from
being called. The D-specific regression observes only A-D, and a parameterized
regression repeats that proof for each possible failure point A-K.

## Remaining runner risks

- The bound runner has not been exercised against a fresh runtime.
- First live observations may expose source/fixture shape differences; those
  must fail closed rather than trigger an in-run oracle change.
- E4 LoanBroker and cover bindings remain source/synthetic-derived.
- Phase-boundary close counts and pseudo-account holding representation still
  require runtime observation.
- LoanSet and LoanPay remain deliberately outside this runner checkpoint.

## Results state and recommendation

- E1: **PASS/FROZEN**
- E2: **PASS/FROZEN**
- E3: **BLOCKED**
- E4-E9: **NOT_ATTEMPTED**

The 1C-B harness verdict is **PASS WITH CONDITIONS**. A separately authorized
frozen-oracle E3/E4 execution may begin. The execution must use the bound oracle
unchanged and stop on the first contradictory observation. E5 and E6 must not
begin.

## Checkpoint 1C-B gates

- B1-B12 and binding regressions: PASS — 29 cases.
- H1-H12 and helper regressions: PASS — 17 cases; frozen file unchanged.
- All spike tests: PASS — 64 cases.
- Repository unit tests: PASS — 17 cases.
- Conformance tests: PASS — 4 cases.
- Lint, formatting, and large-file check: PASS — 67 formatted files checked.
- Strict typecheck: PASS over 25 source files.
- Secrets scan: PASS.
- JSON validation: PASS — 65 files.
- Existing evidence manifests: PASS — Checkpoints 1B, 1B-R, 1C, 1C-R,
  1C-F, and 1C-B.
- Frozen-file immutability: PASS.
- Authorized-path, submodule, and `review/xrpl/` checks: PASS.
- `git diff --check`: PASS.

## Checkpoint 1C-B modified paths

- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`
- `spike/xrpl/scripts/frozen_oracle.py`
- `spike/xrpl/scripts/oracle_fixture.py`
- `spike/xrpl/scripts/run_checkpoint_1c_r.py`
- `spike/xrpl/tests/test_frozen_oracle_runner.py`
- `spike/xrpl/evidence/checkpoint-1c-b/MANIFEST.json`
- `spike/xrpl/evidence/checkpoint-1c-b/fixture-dry-run.json`

---

# Checkpoint 1C-F — frozen-oracle pre-runtime stop

## Outcome

**BLOCKED — the frozen A-K oracle is not fully executable by the committed
runner without prohibited assertion development.**

Checkpoint 1C-F began at clean commit
`42845caf4801894fed5133f5bd33639bfe8cf422`, retained checkpoint-0 ancestry,
and retained the custody and web gitlinks. The pre-runtime audit stopped before
xrpld was started or queried.

**ZERO XRPL protocol transactions were submitted.** No RPC call was made, no
ledger was closed or advanced, and no fresh environment or prerequisite fixture
was created.

## Frozen-oracle integrity

The following committed oracle files were read and hashed before the audit:

| Frozen file | SHA-256 |
| --- | --- |
| `STATE-ASSERTIONS.md` | `836b90b7db41b04a9fdefb78d340f030f798b7650b4945b89ba48990545b416a` |
| `scripts/harness.py` | `098bc81f3358b8c68a235bf8d9c9101f209ff7074232fb9021f645a4b0efae7b` |
| `tests/test_state_assertions.py` | `634880c33181da678d8dead2dac1e932c0dd6aafac44c862fa48259b7710b77c` |

None was modified. H1-H12 remain the committed Checkpoint 1C-H result; no test
or assertion was changed in this checkpoint.

## Exact stop

```text
STOP: the frozen A-K oracle is not fully executable by the committed runner without prohibited assertion development.
```

The checkpoint required every A-K semantic predicate to participate in the
fail-closed runtime classification and prohibited adding assertions or changing
the oracle. Static inspection found that `scripts/run_checkpoint_1c_r.py` does
not encode the complete predeclared oracle:

- A and H do not bind a unique created metadata index into their transition
  assertions or prove the identified created object absent by index in pre-state.
- B, C, and F classify only numeric deltas; their required phase, stable Vault
  relationships, and protected unrelated state are not in the classified list.
- D, E, and G omit the submitting AccountRoot sequence from protected state and
  do not include phase in the classified negative assertion.
- I is not a separately classified relationship step and does not assert that
  the Vault and LoanBroker pseudo-account identities are present and distinct.
- J omits the broker pseudo-account asset holding and the complete stable
  rate/limit/debt comparison.
- K omits the non-owner object set and an explicit semantic absence lookup for
  the attempted LoanBroker identity.
- The runner orders G before F and K before H; I is not separately recorded.

Running that script could therefore produce PASS while required frozen
predicates were unevaluated. Adding those missing checks would be oracle/harness
development, which this checkpoint explicitly prohibits. The required response
was to stop before runtime rather than weaken or silently supplement the oracle.

## Classification and evidence

This is **A — local harness/oracle implementation mismatch**. It is not an XRPL
protocol result, an E1/E2 regression, or runtime evidence against any A-K
expectation. The structured audit is in
`evidence/checkpoint-1c-f/unexpected-result.json`; its SHA-256 manifest covers
the fail-closed evidence.

## Experiment state

- E1: **PASS/FROZEN**
- E2: **PASS/FROZEN**
- E3: **BLOCKED**
- E4-E9: **NOT_ATTEMPTED**

E3 and E4 were not rerun. E5 and E6 must not begin.

## Minimum next checkpoint — not performed

A separately authorized harness-only checkpoint must bind every already
predeclared A-K predicate to executable classification and add synthetic tests
for those bindings without changing the predicate meanings. It must also
produce an execution runner with a fresh evidence destination and the A-K
sequence. Only after that frozen implementation passes review can another
fresh-runtime E3/E4 rerun be authorized.

That work was **not performed**.

## Checkpoint 1C-F gates

- Spike tests: PASS — 35 cases; H1-H12 remain unchanged and passing.
- Repository unit tests: PASS — 17 cases.
- Conformance tests: PASS — 4 cases.
- Lint, formatting, and large-file check: PASS.
- Strict typecheck: PASS over 25 source files.
- Secrets scan: PASS.
- JSON validation: PASS.
- Evidence-manifest verification: PASS.
- Frozen-oracle hash and diff check: PASS.
- Authorized-path, submodule, and `review/xrpl/` checks: PASS.
- `git diff --check`: PASS.

## Checkpoint 1C-F modified paths

- `spike/xrpl/REPORT.md`
- `spike/xrpl/RESULTS.json`
- `spike/xrpl/evidence/checkpoint-1c-f/MANIFEST.json`
- `spike/xrpl/evidence/checkpoint-1c-f/unexpected-result.json`

---

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
