"""Repayment scheduler.

Tech: Python

Responsibility (System architecture table, build plan):
Turns revenue earmarked for repayment into the LoanPay amounts the ledger will actually accept

Owner: Credence Core. Checkpoint 0 stub: no product logic.
"""

from typing import NoReturn


def permitted_payment(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError(
        "Repayment scheduler.permitted_payment: not implemented (Checkpoint 0 stub)"
    )
