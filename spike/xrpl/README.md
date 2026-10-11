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

The minimum distinguishing follow-up experiment is specified in `REPORT.md`
but was intentionally not performed.
