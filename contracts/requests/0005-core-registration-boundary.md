# INTERFACE CHANGE REQUEST 0005: registration requires a history-free facility

- **Contracts:** `contracts/SETTLEMENT-COORDINATOR.md` and `contracts/POLICY-VERIFIER.md` (owner: Credence Core).
- **Requested by:** Integration (San Antonio), Integration I1g, remediating Core finding B-1 (I1f review of `a3ec0a3`).
- **Status:** authorized by Maura on 2026-10-20 for `maurathat/credence-integration-i0`. Core review of the final I1g commit is required before acceptance.

## Change

`SettlementCoordinator.register_facility` refuses `FACILITY_HAS_HISTORY` when the facility's `facility_id` appears in any of these tables (`FACILITY_HISTORY_TABLES`):
- `reservations`
- `used_nonces`
- `accounting_settlements`
- `facility_chain_heads`
- `settlement_disputes`
- `delivery_attempts`
- `no_delivery_releases`
- `late_delivery_disputes`

The check runs in the same `BEGIN IMMEDIATE` transaction as the insert. A refusal writes nothing, and no history is migrated.

Unchanged: canonical objects, signatures, policy, billing, verifier precedence, the existing registration rules for fresh facilities, and the C3B vectors.
