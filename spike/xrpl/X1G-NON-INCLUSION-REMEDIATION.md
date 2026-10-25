# Checkpoint X1g — XRPL non-inclusion proof remediation

## Outcome

X1g closes Salvador X1f finding F1 in XRPL-owned code. No transaction was signed or submitted and no ledger was written.

Base: `f21d9e93100c4e52677da0d9de7328e80328f7b4`.

## Reproduced defect

At the base commit, an adversarial transport stored the target transaction at ledger 101, exposed history `1-100,102-111` during the unbounded `tx` lookup, returned ordinary `txnNotFound`, and backfilled history to `1-111` before `validated_ledger`. `PaymentService` combined those observations and finalized `EXPIRED_NOT_INCLUDED` for the included transaction.

The original request contained only `transaction`, `binary` and `api_version`; it had no `min_ledger`, `max_ledger` or `searched_all` result.

## Correction

- Reconciliation now sends the exact possible inclusion window: submission anchor + 1 through signed `LastLedgerSequence`.
- `RippledTransport` accepts permanent not-found evidence only from `txnNotFound` with literal `searched_all=true`.
- The pinned network, node key, build, validated-ledger identity and history coverage are read before and after the bounded query. Both sides must cover the whole range.
- A `NonInclusionProof` and `credence.xrpl.non-inclusion-evidence.v1` envelope bind the hash, range, source identity and raw responses.
- Final expiry additionally requires the later sender-state observation to match the proof's source, network, node key and build, retain continuous range coverage, and show the original Sequence or TicketSequence still usable.
- X1i supersedes the original ordinary-miss classification: an identity-stable unbounded
  `txnNotFound` is `NOT_FOUND` without proof. It may preserve pre-expiry liveness but can never
  establish permanent non-inclusion.

## Adversarial coverage

`tests/unit/test_xrpl_x1g_non_inclusion.py` covers:

- the ledger-101 gap/backfill reproduction, which remains `UNKNOWN` and later reconciles to `VALIDATED_SUCCESS`;
- a stable history gap and `searched_all=false`;
- omitted `searched_all` on ordinary `txnNotFound`;
- node-key change during a not-found lookup;
- history truncation during a lookup;
- complete, identity-stable bounded non-inclusion, the only case that finalizes `EXPIRED_NOT_INCLUDED`.

Existing successful-payment evidence, exact signed-byte/hash verification, delivered-amount verification, outbox idempotency and submission restrictions are unchanged.

## Remaining limitations

- The pinned rippled 3.4.0 node was not called in X1g. Its unbounded X1e captures prove that ordinary `txnNotFound` omits `searched_all`; the bounded response shape remains documented rather than directly observed.
- rippled 3.4.0 accepts `max_ledger - min_ledger <= 1000`. X1i performs the positive lookup before
  applying that limit; a longer not-found window remains unresolved and cannot expire
  automatically.
- N3, N4 and N5 remain ledger-write blockers. X1g does not authorize live submission, signing, custody, account setup or Core changes.
