# INTERFACE CHANGE REQUEST 0004: Core-authorized delivery; standalone purchase refuses accounted facilities

- **Contract:** `contracts/INFERENCE-GATEWAY-CONTRACT.md` (owner: Inference).
- **Requested by:** Integration (San Antonio), Integration I1e, remediating Inference findings I-1, I-2 and I-4 against `cfc6224`.
- **Status:** authorized by Maura on 2026-10-19 for `maurathat/credence-integration-i0`. Inference review of the remediation commit is required before acceptance.
- **Blocked:** acceptance of the integration branch.

## Changes

1. **The `Gateway(..., delivery_authority=...)` keyword.** It takes Core's `SettlementCoordinator`, or anything with `claim_execution`.
2. **`Gateway.deliver` claims before calling.** Immediately before calling the provider, it claims the persisted attempt through the authority. With no authority it raises `DeliveryNotAuthorized("NO_DELIVERY_AUTHORITY")`; on Core's refusal it raises `DeliveryNotAuthorized` with Core's reason. In both cases no provider is called. A caller-constructed `DeliveryAttempt` is not authority.
3. **`Gateway.purchase` refuses accounted facilities.** It returns `Refused(ACCOUNTED_FACILITY)` when the ledger refuses a registered facility, before any provider call. Its standalone behaviour on unaccounted ledgers is unchanged.
4. **Contract text.** It now states these guarantees and their limits: they bind callers of these interfaces, not direct provider-adapter use.
