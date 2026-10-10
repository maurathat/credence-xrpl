"""Verifier.

Tech: Python library

Responsibility (System architecture table, build plan):
Checks receipts: signatures, bindings, replay, profile match. Produces an immutable decision record

Owner: Credence Core. Checkpoint 0 stub: no product logic.
"""

from typing import NoReturn


def verify(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError("Verifier.verify: not implemented (Checkpoint 0 stub)")
