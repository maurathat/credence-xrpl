"""I2a XRPL boundary: the payment route is truthfully `not_submitted`; the future mapping fails closed."""

import pytest

from credence.api.payment_status import NoXrplPaymentStatus, route_state


def test_no_adapter_reports_not_submitted_with_no_ledger_fields() -> None:
    status = NoXrplPaymentStatus().status("op-" + "0" * 32)
    assert status.state == "not_submitted"
    assert status.source == "no_xrpl_adapter"
    assert (
        status.transaction_hash,
        status.validated_ledger_index,
        status.last_ledger_sequence,
        status.engine_result,
    ) == (None, None, None, None)


@pytest.mark.parametrize(
    ("x0_state", "settlement", "route"),
    [
        (None, None, "not_submitted"),
        ("PREPARED", None, "prepared_not_submitted"),
        ("SUBMITTED", None, "submitted_awaiting_validation"),
        ("PENDING_CONFIRMATION", None, "pending_confirmation"),
        ("VALIDATED_SUCCESS", "XRPL_SETTLED", "validated_success"),
        ("VALIDATED_SUCCESS", None, "unknown_reconciliation_required"),
        ("VALIDATED_FAILURE", None, "validated_failure"),
        ("EXPIRED_NOT_INCLUDED", None, "expired_not_included"),
        ("UNKNOWN", None, "unknown_reconciliation_required"),
        ("MOCK_SETTLED", "XRPL_SETTLED", "unknown_reconciliation_required"),
    ],
)
def test_preflight_mapping_only_confirms_a_validated_settled_success(
    x0_state: str | None, settlement: str | None, route: str
) -> None:
    assert route_state(x0_state, settlement) == route
