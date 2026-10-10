"""Inference gateway.

Tech: Python, x402-style flow carried over from AgentLevy

Responsibility (System architecture table, build plan):
Quotes prices, runs the policy check, forwards the call, collects the receipt

Owner: Inference. Checkpoint 0 stub: no product logic.
"""

from typing import NoReturn


def quote(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError("Inference gateway.quote: not implemented (Checkpoint 0 stub)")


def purchase(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError("Inference gateway.purchase: not implemented (Checkpoint 0 stub)")
