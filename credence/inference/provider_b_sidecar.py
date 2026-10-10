"""Provider B.

Tech: Local model on the Mac mini M4 behind a signing sidecar

Responsibility (System architecture table, build plan):
Serves inference and signs its own receipts

Owner: Inference. Checkpoint 0 stub: no product logic.
"""

from typing import NoReturn


def serve(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError("Provider B.serve: not implemented (Checkpoint 0 stub)")
