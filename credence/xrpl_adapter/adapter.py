"""XRPL adapter.

Tech: Python, `xrpl-py`

Responsibility (System architecture table, build plan):
Vault, loan, payment and key transactions; ledger stream watcher

Owner: XRPL Integration. Checkpoint 0 stub: no product logic.
"""

from typing import NoReturn


def submit(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError("XRPL adapter.submit: not implemented (Checkpoint 0 stub)")


def watch_ledger(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError("XRPL adapter.watch_ledger: not implemented (Checkpoint 0 stub)")
