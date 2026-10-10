"""State store.

Tech: SQLite for the hackathon (Postgres later)

Responsibility (System architecture table, build plan):
Facilities, reservations, receipts, used nonces, events, credit history

Owner: Credence Core. Checkpoint 0 stub: no product logic.
"""

from typing import NoReturn


def open_store(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError("State store.open_store: not implemented (Checkpoint 0 stub)")
