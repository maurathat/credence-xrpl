# 0007. Credence Core C3B: atomic settlement coordinator

_Renumbered from 0005 in Integration I1c (Maura, 2026-10-19), because Inference's local Llama decision already used 0005. The original decisions below are kept as written. Where later decisions supersede them, a marked note says so: §4 by decision 0008, and §1 and §8 by decision 0009._

- Status: Accepted
- Date: 2026-10-19
- Scope: Credence Core checkpoint C3B
- Source: Maura's C3B brief and architectural decisions of 2026-10-19 (Blocker 1: A; Blocker 2: (a) with serialization; additional security correction). Contract: `contracts/SETTLEMENT-COORDINATOR.md`.

## Decisions

1. **SQLite transaction boundary.**
   - Every reserve and every settle is one `BEGIN IMMEDIATE` transaction on the reservation ledger's database. A settle loads the reservation, the approved policy decision, the facility account, the chain head and the replay state; runs the real verifier; and on accept updates drawn, settles the reservation, records the receipt's settlement identity and advances the chain head, all before the single commit.
   - Any exception rolls everything back. Fault injection at four points shows no partial state survives.
   - **Amended by decision 0009 (Integration I1e):** a settle also requires Core's persisted delivery attempt for the reservation and request (`NO_DELIVERY_ATTEMPT`, `ATTEMPT_MISMATCH`). This is checked inside the same transaction, before the verifier. Atomicity and verifier precedence are unchanged.
2. **Drawn accounting.**
   - `facility_accounts` stores each registered facility's limit and drawn amount as exact integers. The stored drawn is authoritative: a reserve presenting a different limit or drawn is refused as stale or conflicting.
   - A settled charge (the verified billed amount, never above the reservation) is added to drawn. The unused hold is released, not spent.
   - The accounting drawn amount is not confirmed XRPL loan principal.
3. **Settlement identity.**
   - SettlementRecord is unchanged and reserved for XRPL settlement, since it requires a real transaction hash.
   - Accounting settlement is recorded in the operational `accounting_settlements` table: receipt digest (primary key), reservation (unique), facility, verifier decision digest, actual amount, time, and state `accounting_settled`.
   - The stages are `delivery_verified`, `accounting_settled` and `xrpl_settled`; only `accounting_settled` is written.
4. **Disputes.**
   - Every verifier rejection is recorded in the operational `settlement_disputes` table. There is no new canonical object.
   - Only an authenticated terminal failure releases the reservation. Authenticated means a valid gateway signature, a valid provider signature at Tier 2, and a receipt for this reservation's request. Terminal means BINDING_MISMATCH, TOKEN_COUNT_EXCEEDS_QUOTE, BILLED_AMOUNT_MISMATCH or CHAIN_BROKEN.
   - Unauthenticated submissions, and RESPONSE_DIGEST_MISMATCH (which rests on the caller's delivered digest), are recorded without release, so an invalid submission cannot free a legitimate pending purchase. Expiry still bounds every hold.
   - **Superseded in part by decision 0008 (Integration I1c, §5):** CHAIN_BROKEN is no longer terminal. It reflects Credence's own receipt sequencing, not the provider's delivery, so a CHAIN_BROKEN rejection keeps the reservation held (expiry bounds it) and keeps the exact signed evidence. The terminal reasons are now BINDING_MISMATCH, TOKEN_COUNT_EXCEEDS_QUOTE and BILLED_AMOUNT_MISMATCH.
5. **Receipt chain.**
   - "The facility's last receipt" is its last accounting-settled receipt, held in `facility_chain_heads`. Only settlement advances it, and the first receipt has a null predecessor, as frozen in C2.
   - The C2 verifier and its precedence are unchanged. The inference gateway must keep at most one receipt in flight per facility, chained to `chain_head`.
6. **Replay protection.** A reservation settles once (status check plus unique `reservation_id`). A receipt settles once (`receipt_digest` primary key). A nonce authorizes one reservation (C2). All of this is persisted and survives restarts.
7. **Expiration.** The C2 boundary is unchanged. At or after `expires_at`, settle releases the hold as expired and refuses, and a delayed receipt can never settle it later.
8. **Signature facts are not bypassable through the coordinator.** It accepts signed objects only and computes the C3A facts itself. The C2 ledger's bare `settle` is refused for accounted facilities, and its `reserve` is held to the stored accounting.
   - **Superseded in part by decision 0009 (Integration I1e):** holding the bare `reserve` to the stored accounting was not enough. It still accepted caller-supplied signature facts, so a forged signature could hold headroom and consume a legitimate nonce (Core finding C-2). The bare `reserve`, `reserve_in_transaction` and `release` now refuse accounted facilities (`USE_SETTLEMENT_COORDINATOR`). The coordinator reserves through an internal path after computing the facts itself.
9. **XRPL exclusions.** No XRPL connection, Payment, LoanSet, LoanPay, vault, Loan Broker, SignerList change or ledger advance. No transaction hash is produced or fabricated, and `authorized_transaction_digest` stays null.

## Consequences

- **Gateway (Inference CP3B).** The gateway must serialize receipt issuance per facility and chain each receipt to `chain_head`. Concurrent in-flight receipts on one facility conflict, and the later one fails `CHAIN_BROKEN`.
- **XRPL settlement (later).** It will need the transaction-digest scheme from the XRPL authorization work, and will then create SettlementRecords for receipts that are already accounting-settled.
- **Unregistered facilities.** These keep the C2 ledger behaviour for compatibility. Production must register every facility before use.
