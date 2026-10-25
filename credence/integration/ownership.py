"""Single-process ownership of an integration database, bound to its canonical identity.

One process at a time may serve an integration database. Ownership is an exclusive, non-blocking
POSIX advisory lock (`fcntl.flock`) on `<canonical database>.owner`, held for the life of the owner.
The operating system drops the lock when the owning process exits or is killed. The process ID
written into the lock file is diagnostic only. A flock lock belongs to an open file description,
so a second owner in the same process is refused too.

Database identity (Integration I1k). Every caller (the service, the offline revocation, tests)
goes through `ProcessOwnership.acquire`, which establishes one identity for the physical file:

1. **Canonical path.** An existing database is resolved with `Path.resolve(strict=True)`, so every
   symlink alias names the same file. A path that does not exist is a fresh database only if it is
   not a symlink (a dangling symlink is refused) and creation was requested; its parent directory
   is resolved strictly and the name appended.
2. **Owner lock.** Derived from the canonical path, never from the caller's spelling, and opened
   with `O_NOFOLLOW`; it must be a regular file with one link.
3. **Verified file.** Under the lock, the database is opened with `O_NOFOLLOW` (a fresh one is
   created with `O_CREAT | O_EXCL`, owner-only), and its device and inode must equal what was
   inspected before the lock was taken. It must be a regular file with exactly one hard link, and
   the caller's path must still resolve to the canonical path. Any mismatch is refused
   (`DatabaseIdentityError`) before ownership is reported.
4. **Anchor.** The verified file stays open for the life of the ownership. `verify()` checks that
   the canonical path still names that same file (device and inode), still one link; callers run
   it at their checkpoints and inside their own write transactions.

The lock is advisory: it binds every caller of this module, not code that opens the SQLite file
directly.

Owner: Integration (San Antonio).
"""

from __future__ import annotations

import fcntl
import os
import stat
from dataclasses import dataclass
from pathlib import Path

OWNER_FILE_MODE = 0o600
DATABASE_FILE_MODE = 0o600


class OwnershipError(RuntimeError):
    """Another process (or another service in this process) owns the database."""


class DatabaseIdentityError(RuntimeError):
    """The database path does not name one verified file. `reason` is a code. Fail closed."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def _race_point(point: str) -> None:
    """Test seam for deterministic race orchestration. Does nothing in production."""


def owner_lock_path(canonical_database: Path) -> Path:
    """The owner lock for a canonical database path (see `canonical_database_path`)."""
    return canonical_database.with_name(canonical_database.name + ".owner")


def canonical_database_path(database: Path, *, allow_create: bool) -> tuple[Path, bool]:
    """(canonical path, fresh). Refuses dangling symlinks and, unless creating, missing files."""
    path = Path(database)
    if path.is_symlink() and not path.exists():
        raise DatabaseIdentityError("DANGLING_SYMLINK", str(path))
    if path.exists():
        return path.resolve(strict=True), False
    if not allow_create:
        raise DatabaseIdentityError("DATABASE_MISSING", str(path))
    try:
        parent = path.parent.resolve(strict=True)
    except FileNotFoundError:
        raise DatabaseIdentityError("PARENT_MISSING", str(path.parent)) from None
    return parent / path.name, True


def _check_regular_single_link(info: os.stat_result, what: str) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise DatabaseIdentityError("NOT_A_REGULAR_FILE", what)
    if info.st_nlink != 1:
        raise DatabaseIdentityError("HARD_LINKED", f"{what} has {info.st_nlink} links")


@dataclass(frozen=True)
class DatabaseIdentity:
    """The canonical path and the device and inode of the one file it names."""

    path: Path
    device: int
    inode: int

    def matches(self, info: os.stat_result) -> bool:
        return (info.st_dev, info.st_ino) == (self.device, self.inode)


class ProcessOwnership:
    def __init__(self, identity: DatabaseIdentity, lock_fd: int, anchor_fd: int) -> None:
        self.identity = identity
        self._lock_fd: int | None = lock_fd
        self._anchor_fd: int | None = anchor_fd

    @classmethod
    def acquire(cls, database: Path, *, allow_create: bool = False) -> ProcessOwnership:
        """Take exclusive ownership of the database's canonical identity, or raise
        OwnershipError (owned elsewhere) or DatabaseIdentityError (unverifiable) without waiting.

        `allow_create` permits initializing a fresh database at a path that does not exist."""
        canonical, fresh = canonical_database_path(database, allow_create=allow_create)
        inspected = None if fresh else os.stat(canonical, follow_symlinks=False)
        if inspected is not None:
            _check_regular_single_link(inspected, str(canonical))
        _race_point("after_resolve")

        lock_fd = cls._lock(canonical)
        anchor_fd: int | None = None
        try:
            _race_point("after_lock")
            anchor_fd = cls._open_verified(canonical, inspected, fresh)
            info = os.fstat(anchor_fd)
            identity = DatabaseIdentity(canonical, info.st_dev, info.st_ino)
            # The caller's spelling must still name this file (no alias retargeted meanwhile).
            try:
                again = Path(database).resolve(strict=True)
            except FileNotFoundError:
                raise DatabaseIdentityError("IDENTITY_CHANGED", "path vanished") from None
            if again != canonical:
                raise DatabaseIdentityError("ALIAS_CHANGED", f"{database} now names {again}")
            ownership = cls(identity, lock_fd, anchor_fd)
            ownership.verify()
        except BaseException:
            if anchor_fd is not None:
                os.close(anchor_fd)
            cls._unlock(lock_fd)
            raise
        os.ftruncate(lock_fd, 0)
        os.write(lock_fd, f"pid={os.getpid()}\n".encode("ascii"))
        return ownership

    @staticmethod
    def _lock(canonical: Path) -> int:
        lock_path = owner_lock_path(canonical)
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, OWNER_FILE_MODE)
        except OSError as exc:
            raise DatabaseIdentityError(
                "LOCK_FILE_UNSAFE", f"{lock_path}: {exc.strerror}"
            ) from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DatabaseIdentityError("LOCK_FILE_UNSAFE", str(lock_path))
            os.fchmod(fd, OWNER_FILE_MODE)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise OwnershipError(f"{canonical} is served by another process") from None
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @staticmethod
    def _open_verified(canonical: Path, inspected: os.stat_result | None, fresh: bool) -> int:
        if fresh:
            try:
                return os.open(
                    canonical,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    DATABASE_FILE_MODE,
                )
            except FileExistsError:
                # Created by someone else between our check and the lock: never adopt it here.
                raise DatabaseIdentityError(
                    "IDENTITY_CHANGED", "appeared during initialization"
                ) from None
        assert inspected is not None
        try:
            fd = os.open(canonical, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise DatabaseIdentityError("IDENTITY_CHANGED", "database disappeared") from None
        except OSError as exc:  # ELOOP: the canonical path became a symlink
            raise DatabaseIdentityError("IDENTITY_CHANGED", exc.strerror or "open failed") from None
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (inspected.st_dev, inspected.st_ino):
            os.close(fd)
            raise DatabaseIdentityError("IDENTITY_CHANGED", "replaced after inspection")
        return fd

    # --- held ownership ----------------------------------------------------------------------

    @property
    def held(self) -> bool:
        return self._lock_fd is not None

    def anchor_stat(self) -> os.stat_result:
        if self._anchor_fd is None:
            raise DatabaseIdentityError("NOT_OWNED")
        return os.fstat(self._anchor_fd)

    def restrict_permissions(self) -> None:
        """Make the owned database owner-only (through the verified file, not the path)."""
        if self._anchor_fd is None:
            raise DatabaseIdentityError("NOT_OWNED")
        os.fchmod(self._anchor_fd, DATABASE_FILE_MODE)

    def verify(self) -> None:
        """The canonical path still names the anchored file, with one link. Fail closed."""
        if self._anchor_fd is None:
            raise DatabaseIdentityError("NOT_OWNED")
        try:
            info = os.stat(self.identity.path, follow_symlinks=False)
        except FileNotFoundError:
            raise DatabaseIdentityError("IDENTITY_CHANGED", "database disappeared") from None
        if not self.identity.matches(info):
            raise DatabaseIdentityError("IDENTITY_CHANGED", "the database path names another file")
        _check_regular_single_link(info, str(self.identity.path))

    def release(self) -> None:
        if self._lock_fd is None:
            return
        lock_fd, self._lock_fd = self._lock_fd, None
        anchor_fd, self._anchor_fd = self._anchor_fd, None
        try:
            self._unlock(lock_fd)
        finally:
            if anchor_fd is not None:
                os.close(anchor_fd)
