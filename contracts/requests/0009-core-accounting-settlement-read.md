# INTERFACE CHANGE REQUEST 0009: read-only accounting-settlement fact for the policy signer

- **Contract:** `contracts/SETTLEMENT-COORDINATOR.md` (owner: Credence Core).
- **Requested by:** XRPL X1a (multisignature and live-ledger preflight), authorized by Maura on 2026-10-24.
- **Status:** open. Nothing in Core was changed. X1a tests use a fake source (`tests/unit/x1a_world.py`, `FakeSettlementSource`).
- **Numbering:** 0005 to 0008 are already used on other unmerged branches (`maurathat/credence-i2a-offline`, `maurathat/credence-integration-i0`), so this request is 0009.
- **Blocked:** wiring the Credence policy signer to Core for the first live tRLUSD provider Payment (tRLUSD is a test token).

## Why

Before it adds its XRPL signature, the policy signer (`credence/signers/policy_signer.py`) re-checks a signed `PaymentAuthorization` statement against Core's own record. It does not trust the statement on its own. It reads Core through `AccountingSettlementSource` (`credence/signers/payment_authorization.py`):

```python
def accounting_settlement(self, receipt_digest: str) -> AccountingSettlementFact | None: ...
def chain_head(self, facility_id: str) -> str | None: ...
```

`SettlementCoordinator.chain_head` already exists. `receipt_settled` returns only a boolean, so the coordinator has no read that returns the settled row.

## Change requested

A read-only coordinator method, or one on `AccountingView`, returning for one `receipt_digest`:

| Field | Source |
| --- | --- |
| `receipt_digest` | `accounting_settlements.receipt_digest` |
| `reservation_id` | `accounting_settlements.reservation_id` |
| `facility_id` | `accounting_settlements.facility_id` |
| `provider_id` | The settled receipt's `provider_id`, which is the reservation's approved quote provider |
| `verifier_decision_digest` | `accounting_settlements.verifier_decision_digest` |
| `actual_amount_micro_trlusd` | `accounting_settlements.actual_amount_micro_trlusd` |
| `settled_at` | `accounting_settlements.settled_at` |
| `state` | `accounting_settlements.state` (always `accounting_settled` today) |

It returns `None` when the receipt is not accounting-settled. The method must not change any table, accounting semantics, canonical object or C3A definition.

## Not requested

- No change to `credence.decision_record.authorized_transaction_digest`. It stays null; the X1a statement carries the authorized digest instead. Populating the decision record would need separate approval.
- No `xrpl_settled` state or `credence.settlement_record`. Recording confirmed XRPL settlement in Core is a later checkpoint. Its input is the X0t outbox event, applied through the X1a idempotent consumer.
