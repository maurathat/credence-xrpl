"""Public X0 XRPL payment adapter surface.

The adapter is deliberately transport-injected and offline at this checkpoint.
It does not construct LoanSet or LoanPay transactions and performs no live
ledger writes.
"""

from credence.xrpl_adapter.journal import PaymentJournal
from credence.xrpl_adapter.preparation import (
    OfflinePaymentPreparer,
    XrplSignedPaymentInspector,
    transaction_hash,
)
from credence.xrpl_adapter.service import (
    PaymentResultVerifier,
    PaymentService,
    ReadOnlyPaymentStatus,
)

__all__ = [
    "OfflinePaymentPreparer",
    "PaymentJournal",
    "PaymentResultVerifier",
    "PaymentService",
    "ReadOnlyPaymentStatus",
    "XrplSignedPaymentInspector",
    "transaction_hash",
]
