# 0012. XRPL X1e: real-node read-only preflight

- Status: Proposed. Awaiting review before any ledger-writing checkpoint.
- Date: 2026-10-24
- Scope: XRPL X1e, read-only against a local pinned rippled 3.4.0. Base `99ca646` (X1c). Amends decisions 0010 and 0011.
- Source: Maura's X1e brief; Salvador's X1c acceptance notes (N1–N5). Report: `spike/xrpl/X1E-REAL-NODE-PREFLIGHT.md`.

## Decisions

1. **Pinned identity for authority (N1).** Any transport read that can authorize signing, anchor expiry or establish settlement requires a configured node public key. Unpinned reads are diagnostic only and return no evidence object.
2. **Identity stable across a lookup (N2).** The node identity is re-read after the evidence and must be unchanged and still cover the transaction's ledger.
3. **Stable standalone identity comes from a runtime node seed.** The seed is held only in a dedicated Docker volume, never in Git, logs or the host workspace. The historical config alone produces a new node key on every restart.
4. **Real responses are committed evidence.** The read-only API v2 responses are committed with a SHA-256 manifest and replayed through the transport in the test suite.
5. **Found-transaction shapes stay unconfirmed.** Found `tx` responses could not be observed without a transaction. They remain verified only against the fake and must be confirmed on a real validated Payment before settlement relies on them.

## Consequences

- N3, N4 and N5 remain mandatory before ledger writes, together with Core request 0009, key custody, ResearchBot integration, token and trust-line setup, funding and the production settlement effect.
- `infra/rippled/` is still a placeholder. The pinned node exists only as the historical image plus X1e's runtime volumes.
