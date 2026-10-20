"""Single-process ownership of an integration database.

One process at a time may serve an integration database. Ownership is an exclusive, non-blocking
POSIX advisory lock (`fcntl.flock`) on `<database>.owner`, held for the life of the owner. The
operating system drops the lock when the owning process exits or is killed, so a crashed owner
never leaves a stale lock behind. The process ID written into the lock file is diagnostic only and
is never trusted. A flock lock belongs to an open file description, so a second owner in the same
process is refused too.

The lock is advisory: it binds every IntegrationService, not code that opens the SQLite file
directly.

Owner: Integration (San Antonio).
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

OWNER_FILE_MODE = 0o600


class OwnershipError(RuntimeError):
    """Another process (or another service in this process) owns the database."""


def owner_lock_path(database: Path) -> Path:
    return database.with_name(database.name + ".owner")


class ProcessOwnership:
    def __init__(self, lock_path: Path, fd: int) -> None:
        self._lock_path = lock_path
        self._fd: int | None = fd

    @classmethod
    def acquire(cls, database: Path) -> ProcessOwnership:
        """Take exclusive ownership of `database`, or raise OwnershipError without waiting."""
        lock_path = owner_lock_path(database)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, OWNER_FILE_MODE)
        try:
            os.fchmod(fd, OWNER_FILE_MODE)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise OwnershipError(f"{database} is served by another process") from None
        except BaseException:
            os.close(fd)
            raise
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        return cls(lock_path, fd)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
