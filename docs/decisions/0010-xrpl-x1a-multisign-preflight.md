# 0010. XRPL X1a: multisignature and live-ledger preflight

- Status: Proposed. Awaiting Maura's X1a review.
- Date: 2026-10-24
- Scope: XRPL X1a, offline implementation and preflight. Base `ada4d6d` (X0t).
- Source: Maura's X1a brief; `spike/xrpl/X0-READINESS.md`; build plan ("Accounts and keys", "Security and key management"). Report: `spike/xrpl/X1A-PREFLIGHT.md`. Request: `contracts/requests/0009-core-accounting-settlement-read.md`.

## Decisions

1. **Unsigned-transaction identity.** The authorized digest is the SHA-512Half of the XRPL single-signing serialization of the unsigned multisign form: empty `SigningPubKey`, no `TxnSignature`, no `Signers`. The scheme is `xrpl-stx-sha512half-v1`. Both signers sign that exact transaction, and verification recomputes the digest from the signed bytes with `Signers` removed.
2. **Authorization carrier.** The digest is carried by a new operational `PaymentAuthorization` statement, Ed25519 over a domain-separated Kessai (JCS (RFC 8785) + NFC) form. It is not carried by `credence.decision_record.authorized_transaction_digest`, which stays frozen null. No C1/C3A object, C3A role or signature definition changes. Moving the digest into the decision record would need separate approval.
3. **Independent policy-signer check.** The policy signer re-reads Core's accounting settlement and chain head, rebuilds the canonical Payment from the statement, and checks validated ledger state before signing. A statement alone is never sufficient.
4. **One transaction per receipt.** The policy signer durably records the receipt, operation and authorization against one unsigned digest before signing, so a second transaction paying the same receipt is refused across restarts and concurrency.
5. **Master and regular keys.** The agent payment account must have no RegularKey. Its master-key state is an explicit policy. `OWNER_RETAINED` is the build plan's setup: the owner's Ledger device holds the borrower account's key. While that key is enabled, the owner can authorize alone. Signers sign only with their own master keys.
6. **Trusted transport.** Loopback-only JSON-RPC, API v2, every read pinned to one validated ledger hash, raw responses kept. Submission is disabled by configuration, and the X0t service still refuses non-offline transports.
7. **One settlement-effect path.** The outbox event, applied by a durable idempotent consumer keyed by `event_id`, is the only live settlement-effect authority. The legacy claim bit is not.

## Consequences

- One live tRLUSD (test token) Payment still needs a separately authorized checkpoint, the pinned rippled configuration, Core request 0009, key custody, ResearchBot signing integration, an enforced live submission authority and a Core-side settlement effect (`spike/xrpl/X1A-PREFLIGHT.md`, "Remaining dependencies").
- Ledger hardware signing and a QuickNode transport are documented as later X1 adapters, not implemented.
