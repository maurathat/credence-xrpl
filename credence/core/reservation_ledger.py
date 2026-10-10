"""Reservation ledger.

Tech: Python, in the state store

Responsibility (System architecture table, build plan):
Internal accounting holds against a facility for pending purchases; handles concurrent requests and failed deliveries. Not funds locked on XRPL

Owner: Credence Core. Checkpoint 0 stub: no product logic.
"""

from typing import NoReturn


def reserve(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError("Reservation ledger.reserve: not implemented (Checkpoint 0 stub)")


def settle(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError("Reservation ledger.settle: not implemented (Checkpoint 0 stub)")


def release(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError("Reservation ledger.release: not implemented (Checkpoint 0 stub)")
