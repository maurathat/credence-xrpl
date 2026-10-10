"""Credence API.

Tech: Python, FastAPI

Responsibility (System architecture table, build plan):
Thin HTTP and WebSocket layer over the core; streams live events to the UI

Owner: API/UI (no decision logic here). Checkpoint 0 stub: no product logic.
"""

from typing import NoReturn


def create_app(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError("Credence API.create_app: not implemented (Checkpoint 0 stub)")
