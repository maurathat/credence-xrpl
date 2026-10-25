# 0013. XRPL X1g: bounded non-inclusion proof

- Status: Implemented. Awaiting Salvador's targeted acceptance.
- Date: 2026-10-24
- Scope: XRPL X1g, offline remediation. Base `f21d9e93100c4e52677da0d9de7328e80328f7b4`.
- Source: Salvador X1f finding F1. Report: `spike/xrpl/X1G-NON-INCLUSION-REMEDIATION.md`.

## Decisions

1. **A plain `txnNotFound` is not evidence of permanent non-inclusion.** Reconciliation queries the exact transaction hash with `min_ledger` equal to the trusted pre-submission anchor plus one and `max_ledger` equal to the signed `LastLedgerSequence`.
2. **`searched_all` is explicit.** Only literal boolean `searched_all=true` from that bounded response is accepted. It is never inferred from `txnNotFound`, `complete_ledgers`, sender state or a later backfill.
3. **One identity-stable observation.** The transport reads and pins network ID, node public key, build version, validated-ledger identity and complete history before and after the bounded lookup. Both observations must cover the whole inclusion window. Any gap, regression, identity change or relevant history change is unavailable evidence.
4. **The proof remains independently bound.** `NonInclusionProof` binds the transaction hash, exact ledger range, source, network, node key, build, validated ledger and raw request/response evidence. `PaymentService` cross-checks it against the persisted payment, submission anchor and a later sender-state observation from the same pinned identity before finalizing expiry.
5. **Found transactions are unchanged.** Exact signed bytes, transaction hash, validated-ledger inclusion, result and delivered-amount checks remain the finality path for found payments. Submission remains disabled by the existing X0/X1 boundaries.

## rippled 3.4.0 assumptions

- X1i's direct source inspection supersedes the earlier range wording: API v2 `tx` accepts
  `min_ledger` and `max_ledger` together when `max_ledger - min_ledger <= 1000`, a closed interval
  of at most 1,001 ledger indexes in rippled 3.4.0.
- A not-found response reports `searched_all` only for a bounded request. `true` means every ledger in that requested range was searched; false or omission is inconclusive.
- A found transaction can be returned even outside the requested range, so found-result verification continues to enforce the persisted submission anchor and `LastLedgerSequence` independently.
- X1e's real-node captures contain only unbounded `txnNotFound` responses and therefore do not validate the bounded `searched_all` shape. The X1g bounded path is tested against the deterministic rippled model and must be confirmed read-only on the pinned 3.4.0 node before a ledger-writing checkpoint.

## Consequences

- The ledger-101 history-gap/backfill attack remains `UNKNOWN`; it can later reconcile to the real validated transaction.
- Missing, truncated, contradictory or changing history cannot produce `EXPIRED_NOT_INCLUDED`.
- N3, N4, N5, the real found-transaction shape and all previously documented live-write prerequisites remain open.
