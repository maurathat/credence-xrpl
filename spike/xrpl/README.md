# spike/xrpl

Owner: XRPL spike workstream (Codex).

Checkpoint 1 stopped fail-closed at its initial network/amendment gate. No
protocol transaction was submitted.

The stop was caused by a local validation-harness error: the harness used
non-matching amendment IDs and looked for them at the top level of the
`feature` response instead of under `result.features`. The unchanged pinned
server's response shows that all eight required amendments exist, are
supported, and are enabled. The protocol experiments were not continued.

See:

- `REPORT.md` for the stop report and classification.
- `ENVIRONMENT.md` for the pinned build and genesis/configuration details.
- `RESULTS.json` for E1-E9 machine-readable status.
- `evidence/unexpected-result.json` for the original false-negative record.
- `evidence/amendments.json` for the complete 106-amendment inventory.
- `evidence/server-info.json` for the stopped server state.

Checkpoint 1A subsequently corrected only the validation harness and reran E1.
E1 now passes; the original false-negative BLOCKED conclusion is retired.
E2-E9 remain unattempted. See the recovery section appended to `REPORT.md` and
`evidence/e1-validation.json` for the machine-readable result.

Checkpoint 1B then stopped at its required E1 regression gate: the new
on-ledger amendment cross-check sent an invalid fixed-object selector and
received `malformedRequest`. No protocol transaction was submitted. E2-E9 are
`NOT_ATTEMPTED`; see the Checkpoint 1B section of `REPORT.md` and
`evidence/checkpoint-1b/unexpected-result.json`.

Checkpoint 1B-R repaired that request using the pinned source's fixed-location
selector, `amendments: true`. A fresh E1 run passed: `feature` and the
Amendments ledger object agreed on all eight required amendments before and
after two reproducible closes. E1 is now frozen; E2-E9 remain
`NOT_ATTEMPTED`. Reproduce the environment with `scripts/start_ledger.sh` and
see `evidence/checkpoint-1b-r/` for raw evidence.

T1 is a separate public native XRPL Testnet connectivity proof. Its offline,
fail-closed plan and isolated scripts are documented in
`T1-PUBLIC-TESTNET-PLAN.md`; the B1/B2/C1–C4 offline remediation evidence is in
`T1A-REMEDIATION.md`. T1 is not governed by Credence procurement
authorization, does not change the product's local-ledger claim, and remains
unexecuted pending explicit T1 execution authorization.
