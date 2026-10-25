"""Public XRPL adapter surface smoke test."""

from credence.xrpl_adapter import PaymentService, TransactionState


def test_public_payment_adapter_surface_imports() -> None:
    assert PaymentService.__doc__
    assert TransactionState.PREPARED.value == "PREPARED"
