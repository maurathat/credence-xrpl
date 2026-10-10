"""Loan Broker signer.

Tech: Python, key in 1Password

Responsibility (System architecture table, build plan):
Supplies the counterparty signature on a loan only when the owner's Ledger approval is present and valid

Owner: Credence Core (kept separate from the engine to isolate keys). Checkpoint 0 stub: no product logic.
"""

from typing import NoReturn


def sign_loan(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError("Loan Broker signer.sign_loan: not implemented (Checkpoint 0 stub)")
