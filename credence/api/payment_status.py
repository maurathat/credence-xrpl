"""XRPL payment-status boundary for the I2a product API. Read only; performs no financial operation.

I2a does not connect any XRPL code to the payment path. The X0 payment-readiness candidate
(aa236e1) is not approved for integration and is not imported here. The route therefore answers
from `NoXrplPaymentStatus`: every operation is truthfully `not_submitted`, with no transaction
hash, ledger index or engine result.

`PaymentStatusSource` is the interface a future, reviewed, read-only X0s adapter would implement.
`route_state` records the state mapping proposed in the I2-0 preflight (section 5) so that the
vocabulary is fixed before any adapter exists; it is not wired to anything. Only a validated
success whose settlement status is `XRPL_SETTLED` maps to `validated_success`, and mock-transport
states must never be passed to it as ledger states.

Accounting settlement (Core) is not XRPL settlement, and nothing here reads or changes Core.

Owner: API/UI (I2a integration). Provisional.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

NOT_SUBMITTED = "not_submitted"


@dataclass(frozen=True)
class PaymentStatusView:
    state: str
    source: str
    detail: str
    transaction_hash: str | None = None
    validated_ledger_index: int | None = None
    last_ledger_sequence: int | None = None
    engine_result: str | None = None


class PaymentStatusSource(Protocol):
    def status(self, operation_id: str) -> PaymentStatusView: ...


class NoXrplPaymentStatus:
    """I2a: no XRPL adapter is connected, so no payment exists for any operation."""

    def status(self, operation_id: str) -> PaymentStatusView:
        return PaymentStatusView(
            state=NOT_SUBMITTED,
            source="no_xrpl_adapter",
            detail="No XRPL transaction is built, signed or submitted in I2a. No XRPL adapter "
            "is connected; the reviewed read-only payment status (X0s) is a future dependency.",
        )


_ROUTE_STATES = {
    "PREPARED": "prepared_not_submitted",
    "SUBMITTED": "submitted_awaiting_validation",
    "PENDING_CONFIRMATION": "pending_confirmation",
    "VALIDATED_FAILURE": "validated_failure",
    "EXPIRED_NOT_INCLUDED": "expired_not_included",
    "UNKNOWN": "unknown_reconciliation_required",
}


def route_state(x0_state: str | None, settlement_status: str | None) -> str:
    """Preflight mapping from an X0 PaymentRecord state (None: no record) to the route state.
    Not wired in I2a. Unrecognized input fails closed to `unknown_reconciliation_required`."""
    if x0_state is None:
        return NOT_SUBMITTED
    if x0_state == "VALIDATED_SUCCESS":
        return (
            "validated_success"
            if settlement_status == "XRPL_SETTLED"
            else "unknown_reconciliation_required"
        )
    return _ROUTE_STATES.get(x0_state, "unknown_reconciliation_required")
