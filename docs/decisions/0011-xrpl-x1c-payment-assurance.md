# 0011. XRPL X1c: payment assurance remediation

- Status: Proposed. Awaiting the X1d re-review by Salvador and the Core authorization owner.
- Date: 2026-10-24
- Scope: XRPL X1c, offline. Base `607cc2a` (X1a). Amends decision 0010.
- Source: Maura's X1c brief; Salvador's X1b findings B1–B5 and C1–C3; Maura's approval of C1 option A. Report: `spike/xrpl/X1C-REMEDIATION.md`.

## Decisions

1. **Delivered amount.** The binary metadata is authoritative. Its `DeliveredAmount` is used, falling back to the signed `Amount` for a successful Payment without `tfPartialPayment`. The JSON `delivered_amount` must agree exactly; any disagreement is `EVIDENCE_INCONSISTENT`, and nothing settles.
2. **Identity on every lookup.** Each lookup re-verifies the node's network, its pinned build and, when pinned, its node key. The transaction's ledger must be validated, at or below the validated ledger, inside complete history, and hash-consistent. The build pin is now required.
3. **Policy signer reads the ledger itself.** It obtains state through its own trusted source. A caller's context is only compared with it, never trusted.
4. **Signer-account RegularKey refused.** The key is extra signing authority for that signer, so a funded signer account holding one is not accepted.
5. **One settlement-effect authority.** The outbox event, applied idempotently by `event_id`, is the only path. The legacy claim is refused whenever an outbox event exists, and stored claim history is kept.
6. **Unexpected delivery is its own journal state.** `VALIDATED_UNEXPECTED_DELIVERY` lives in journal schema version 3. Version-2 journals are refused unchanged, with no automatic migration (C1 option A, approved by Maura on 2026-10-24).
7. **Canonical signatures.** Rippled's fully-canonical rule applies before verification.
8. **No redirects.** The pinned loopback client never follows an HTTP redirect.

## Consequences

- Any version-2 journal needs a separately approved recovery procedure before its payments can be reconciled under X1c code.
- The C4 and C5 deferrals, live submission, custody and the Core interface request 0009 remain open.
