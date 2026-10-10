"""Settlement coordinator.

Tech: Python

Responsibility (System architecture table, build plan):
Consumes verifier decisions and executes the permitted payment: a co-signed provider payment, or release of the reservation

Owner: Credence Core. Checkpoint 0 stub: no product logic.
"""

from typing import NoReturn


def settle(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError("Settlement coordinator.settle: not implemented (Checkpoint 0 stub)")
