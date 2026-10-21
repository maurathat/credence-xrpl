# INTERFACE CHANGE REQUEST 0007: agent-scoped operation and accounting reads (CR-3, CR-4)

- **Contracts:** `contracts/INTEGRATION-FLOW.md` (owner: Integration); `contracts/SETTLEMENT-COORDINATOR.md` (owner: Credence Core) for the two Core reads.
- **Requested by:** I2a offline integration (`maurathat/credence-i2a-offline`), from the I2-0 preflight CR-3 and CR-4.
- **Status:** implemented provisionally on the I2a branch as authorized by Maura's I2a brief (2026-10-20). Integration and Core review required before acceptance. `SETTLEMENT-COORDINATOR.md` is not edited here; the Core owner should record the two reads if accepted.

## Change

Read only; no write path changes.
1. Integration: `operations_for_agent(key_id, limit=, after=)`, `operation_for_receipt(digest)`, `lifecycle_events(operation_id)` (persisted timestamps only; CR-5 history persistence is not implemented), `public_key(key_id)`. Journal indexes by agent and by receipt.
2. `AccountingView`: `accounting_settlement(receipt_digest)`, `policy_decision_digest(reservation_id)`, `decision_record(digest)`.
3. Core: `SettlementCoordinator.accounting_settlement(receipt_digest) -> AccountingSettlement | None` and `ReservationLedger.decision_record_digest(reservation_id) -> str | None`, both SELECT-only.
