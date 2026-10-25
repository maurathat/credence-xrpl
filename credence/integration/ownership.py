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

Trusted directory boundary (Integration I1m). Identity checks see a replacement only while it is
observable: a swap-and-restore completed entirely between two checks can evade them. Credence's
supported deployment therefore requires that no untrusted OS principal can replace or redirect the
database, its lock file or SQLite's journal. `resolve_trusted` enforces a conservative
policy before ownership is taken, and the database directory is re-checked at every `verify()`:

- **Database directory** (it holds the database, the owner lock and SQLite's journal): owned by
  this effective user; no group or other write permission (sticky or not); no ACL entry that
  allows writing, adding or deleting entries.
- **Every ancestor** of it: owned by this effective user or root; no write-granting ACL entry; if
  group- or other-writable, only with the sticky bit set and the next path component owned by
  this user or root (so no one else can rename or delete it).
- **Every traversed symlink** (I1o): `resolve_trusted` walks each component from the root, on the
  caller's path and inside every link target, on every hop. Each link must be owned by this user
  or root and sit in a directory that passes the ancestor rule, so no chain can cross a directory
  someone else could redirect. Loops, dangling targets and links that change while read are
  refused, and the walk must agree with the OS's resolution.
- **Files** (I1o): the database and its lock file, through their open descriptors: owned by this
  effective user, one link, no group or other write bit, no ACL entry granting write, delete,
  attribute, security or ownership changes. Checked before either file is changed.
- **Revalidation**: `verify()` re-runs the walk, the file checks and the lock identity at every
  checkpoint.

Deny-only ACL entries (such as macOS's default `everyone deny delete` on home directories) are
accepted. ACLs that cannot be read are refused. Only macOS and Linux are supported. Code running
with this user's own filesystem authority is outside the boundary.

The lock is advisory: it binds every caller of this module, not code that opens the SQLite file
directly.

Owner: Integration (San Antonio).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import fcntl
import os
import stat
import sys
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


def _effective_uid() -> int:
    """The trusted owner (a seam so tests can model a foreign owner without a second account)."""
    return os.geteuid()


# --- trusted directory boundary (I1m) -------------------------------------------------------------

# macOS ACL permissions that let a principal change a directory's entries or a file's contents.
_MODIFYING_ACL_PERMISSIONS = frozenset(
    {
        "add_file",
        "add_subdirectory",
        "delete",
        "delete_child",
        "write",
        "append",
        "writeattr",
        "writeextattr",
        "writesecurity",
        "chown",
    }
)
_ACL_TYPE_EXTENDED = 0x00000100


_LIBC: ctypes.CDLL | None = None


def _libc() -> ctypes.CDLL:
    global _LIBC
    if _LIBC is None:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libc.acl_get_file.restype = ctypes.c_void_p
        libc.acl_get_file.argtypes = [ctypes.c_char_p, ctypes.c_int]
        libc.acl_get_fd_np.restype = ctypes.c_void_p
        libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        libc.acl_to_text.restype = ctypes.c_void_p
        libc.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        libc.acl_free.argtypes = [ctypes.c_void_p]
        _LIBC = libc
    return _LIBC


def _darwin_acl_text(target: Path | int) -> str | None:
    """The extended ACL of a path (without following a final symlink is not needed: callers pass
    directories they walked, or open file descriptors) as text, or None if there is none."""
    libc = _libc()
    if isinstance(target, int):
        handle = libc.acl_get_fd_np(target, _ACL_TYPE_EXTENDED)
    else:
        handle = libc.acl_get_file(os.fsencode(target), _ACL_TYPE_EXTENDED)
    if not handle:
        if ctypes.get_errno() == errno.ENOENT:
            return None  # no extended ACL
        raise DatabaseIdentityError("ACL_UNREADABLE", str(target))
    try:
        text = libc.acl_to_text(handle, None)
        if not text:
            raise DatabaseIdentityError("ACL_UNREADABLE", str(target))
        try:
            return ctypes.string_at(text).decode("utf-8", "replace")
        finally:
            libc.acl_free(text)
    finally:
        libc.acl_free(handle)


def acl_grants_modification(target: Path | int) -> bool:
    """Whether an ACL entry may let some principal modify `target` (a path or an open file
    descriptor): write, append, delete, add or delete entries, attributes, security or owner.
    Deny-only and read-only entries do not. Unreadable or unsupported: refused."""
    if sys.platform == "darwin":
        text = _darwin_acl_text(target)
        if text is None:
            return False
        for line in text.splitlines()[1:]:  # first line is the "!#acl 1" header
            fields = line.split(":")
            if len(fields) < 6:
                if line.strip():
                    raise DatabaseIdentityError("ACL_UNREADABLE", str(target))
                continue
            kind, permissions = fields[4], set(fields[5].split(","))
            if kind != "deny" and permissions & _MODIFYING_ACL_PERMISSIONS:
                return True
        return False
    if sys.platform.startswith("linux"):
        try:
            names = os.listxattr(target)
        except OSError:
            raise DatabaseIdentityError("ACL_UNREADABLE", str(target)) from None
        return any(n in ("system.posix_acl_access", "system.posix_acl_default") for n in names)
    raise DatabaseIdentityError("UNSUPPORTED_PLATFORM", sys.platform)


def _trusted_owner(uid: int) -> bool:
    return uid in (_effective_uid(), 0)


def check_database_directory(directory: Path) -> None:
    """The canonical database directory: ours, not group/other writable, no write-granting ACL."""
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode):
        raise DatabaseIdentityError("NOT_A_DIRECTORY", str(directory))
    if info.st_uid != _effective_uid():
        raise DatabaseIdentityError("UNTRUSTED_DIRECTORY_OWNER", str(directory))
    if info.st_mode & 0o022:
        raise DatabaseIdentityError(
            "WRITABLE_DATABASE_DIRECTORY", f"{directory} {oct(stat.S_IMODE(info.st_mode))}"
        )
    if acl_grants_modification(directory):
        raise DatabaseIdentityError("DIRECTORY_ACL", str(directory))


def _check_container(directory: Path, child_owner: int) -> None:
    """A directory traversed on the way to the database, holding an entry owned by
    `child_owner`: ours or root's; if group/other writable, only sticky with a trusted entry."""
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode):
        raise DatabaseIdentityError("NOT_A_DIRECTORY", str(directory))
    if not _trusted_owner(info.st_uid):
        raise DatabaseIdentityError("UNTRUSTED_DIRECTORY_OWNER", str(directory))
    if info.st_mode & 0o022 and not (info.st_mode & stat.S_ISVTX and _trusted_owner(child_owner)):
        raise DatabaseIdentityError(
            "WRITABLE_DIRECTORY", f"{directory} {oct(stat.S_IMODE(info.st_mode))}"
        )
    if acl_grants_modification(directory):
        raise DatabaseIdentityError("DIRECTORY_ACL", str(directory))


MAX_SYMLINK_HOPS = 40


def resolve_trusted(database: Path, *, allow_create: bool) -> tuple[Path, bool]:
    """(canonical path, fresh), by walking every component actually traversed (I1o, V1).

    `Path.resolve()` is not trusted on its own. Starting from the root, each component is looked
    up with lstat in a directory that has already been checked, and that directory must meet the
    container rule for the entry. A symlink must be owned by this user or root; its target's
    components are walked the same way (absolute targets restart at the root), so every
    directory on every hop is checked, however deeply links nest. More than
    `MAX_SYMLINK_HOPS` links is a loop (`SYMLINK_LOOP`); a link whose target is missing is
    `DANGLING_SYMLINK`. The final component may be missing only if it is the caller's own last
    name and creation is allowed (fresh). The database directory must then meet the stricter
    database-directory rule, and the walk must agree with the OS's own resolution
    (`PATH_CHANGED` otherwise: something moved while it was being validated)."""
    requested = Path(database)
    raw = requested if requested.is_absolute() else Path(os.getcwd()) / requested
    pending: list[tuple[str, bool]] = [(part, False) for part in raw.parts[1:]]
    current = Path(raw.anchor or "/")
    hops = 0
    final: Path | None = None
    fresh = False
    while pending:
        name, from_link = pending.pop(0)
        if name in ("", "."):
            continue
        if name == "..":
            current = current.parent
            continue
        if final is not None:
            raise DatabaseIdentityError("NOT_A_DIRECTORY", str(final))
        candidate = current / name
        try:
            entry = os.lstat(candidate)
        except FileNotFoundError:
            if pending:
                raise DatabaseIdentityError("PARENT_MISSING", str(candidate)) from None
            if from_link:
                raise DatabaseIdentityError("DANGLING_SYMLINK", str(candidate)) from None
            if not allow_create:
                raise DatabaseIdentityError("DATABASE_MISSING", str(candidate)) from None
            check_database_directory(current)  # where the fresh database would live
            final, fresh = candidate, True
            continue
        if stat.S_ISREG(entry.st_mode):
            check_database_directory(current)  # the stricter rule for the database's own home
        _check_container(current, entry.st_uid)
        if stat.S_ISLNK(entry.st_mode):
            hops += 1
            if hops > MAX_SYMLINK_HOPS:
                raise DatabaseIdentityError("SYMLINK_LOOP", str(candidate))
            if not _trusted_owner(entry.st_uid):
                raise DatabaseIdentityError("UNTRUSTED_SYMLINK", str(candidate))
            target = os.readlink(candidate)
            again = os.lstat(candidate)
            if (again.st_dev, again.st_ino, again.st_mtime_ns) != (
                entry.st_dev,
                entry.st_ino,
                entry.st_mtime_ns,
            ):
                raise DatabaseIdentityError("PATH_CHANGED", f"{candidate} changed while read")
            link = Path(target)
            expanded = [(part, True) for part in link.parts[1 if link.is_absolute() else 0 :]]
            if not expanded and not link.is_absolute():
                raise DatabaseIdentityError("DANGLING_SYMLINK", str(candidate))
            if link.is_absolute():
                current = Path(link.anchor)
            pending = expanded + pending
        elif stat.S_ISDIR(entry.st_mode):
            current = candidate
        else:
            final = candidate  # must be the last component (checked on the next iteration)
    if final is None:
        raise DatabaseIdentityError("NOT_A_REGULAR_FILE", str(current))
    check_database_directory(final.parent)
    if not fresh:
        try:
            observed = requested.resolve(strict=True)
        except (FileNotFoundError, RuntimeError):
            raise DatabaseIdentityError("PATH_CHANGED", str(requested)) from None
        if observed != final:
            raise DatabaseIdentityError("PATH_CHANGED", f"{requested} now resolves to {observed}")
    return final, fresh


def owner_lock_path(canonical_database: Path) -> Path:
    """The owner lock for a canonical database path (see `resolve_trusted`)."""
    return canonical_database.with_name(canonical_database.name + ".owner")


def canonical_database_path(database: Path, *, allow_create: bool) -> tuple[Path, bool]:
    """Compatibility name for `resolve_trusted` (I1k)."""
    return resolve_trusted(database, allow_create=allow_create)


def check_file_security(fd: int, what: str) -> os.stat_result:
    """An open database or lock file (I1o, V2): ours, one link, no group/other write bit, and no
    ACL entry letting anyone write, delete, or change its permissions or owner. Checked through
    the descriptor, so nothing is changed before its security is established."""
    info = os.fstat(fd)
    _check_regular_single_link(info, what)
    if info.st_uid != _effective_uid():
        raise DatabaseIdentityError("NOT_OWNER", f"{what} has another owner")
    if info.st_mode & 0o022:
        raise DatabaseIdentityError("FILE_PERMISSIONS", f"{what} {oct(stat.S_IMODE(info.st_mode))}")
    if acl_grants_modification(fd):
        raise DatabaseIdentityError("FILE_ACL", what)
    return info


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
    def __init__(
        self, identity: DatabaseIdentity, lock_path: Path, lock_fd: int, anchor_fd: int
    ) -> None:
        self.identity = identity
        self._lock_path = lock_path
        self._lock_fd: int | None = lock_fd
        self._anchor_fd: int | None = anchor_fd

    @classmethod
    def acquire(cls, database: Path, *, allow_create: bool = False) -> ProcessOwnership:
        """Take exclusive ownership of the database's canonical identity, or raise
        OwnershipError (owned elsewhere) or DatabaseIdentityError (unverifiable or untrusted)
        without waiting.

        `allow_create` permits initializing a fresh database at a path that does not exist."""
        canonical, fresh = resolve_trusted(Path(database), allow_create=allow_create)
        inspected = None if fresh else os.stat(canonical, follow_symlinks=False)
        if inspected is not None:
            _check_regular_single_link(inspected, str(canonical))
        _race_point("after_resolve")

        lock_path = owner_lock_path(canonical)
        lock_fd = cls._lock(lock_path, canonical)
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
            ownership = cls(identity, lock_path, lock_fd, anchor_fd)
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
    def _lock(lock_path: Path, canonical: Path) -> int:
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, OWNER_FILE_MODE)
        except OSError as exc:
            raise DatabaseIdentityError(
                "LOCK_FILE_UNSAFE", f"{lock_path}: {exc.strerror}"
            ) from None
        try:
            try:
                check_file_security(fd, f"lock {lock_path}")  # before any change to it
            except DatabaseIdentityError as exc:
                raise DatabaseIdentityError(
                    "LOCK_FILE_UNSAFE", f"{exc.reason}: {lock_path}"
                ) from None
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # The name must still lead to the file we locked (not deleted and recreated).
            info = os.fstat(fd)
            try:
                named = os.lstat(lock_path)
            except FileNotFoundError:
                raise DatabaseIdentityError("LOCK_FILE_UNSAFE", f"{lock_path} vanished") from None
            if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
                raise DatabaseIdentityError("LOCK_FILE_UNSAFE", f"{lock_path} was replaced")
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
                fd = os.open(
                    canonical,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    DATABASE_FILE_MODE,
                )
            except FileExistsError:
                # Created by someone else between our check and the lock: never adopt it here.
                raise DatabaseIdentityError(
                    "IDENTITY_CHANGED", "appeared during initialization"
                ) from None
        else:
            assert inspected is not None
            try:
                fd = os.open(canonical, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                raise DatabaseIdentityError("IDENTITY_CHANGED", "database disappeared") from None
            except OSError as exc:  # ELOOP: the canonical path became a symlink
                raise DatabaseIdentityError(
                    "IDENTITY_CHANGED", exc.strerror or "open failed"
                ) from None
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (inspected.st_dev, inspected.st_ino):
                os.close(fd)
                raise DatabaseIdentityError("IDENTITY_CHANGED", "replaced after inspection")
        try:
            check_file_security(fd, f"database {canonical}")  # before any change to it
        except BaseException:
            os.close(fd)
            raise
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
        """Revalidate everything the ownership rests on (I1k, I1m, I1o). Fail closed.

        - the trusted walk of the canonical path still succeeds and still yields it (every
          traversed directory still trusted; database directory still private);
        - the canonical path still names the anchored file, one link, ours, no group/other write
          bit, no modifying ACL;
        - the lock path still names the locked file, with the same file checks."""
        if self._anchor_fd is None or self._lock_fd is None:
            raise DatabaseIdentityError("NOT_OWNED")
        walked, _ = resolve_trusted(self.identity.path, allow_create=False)
        if walked != self.identity.path:
            raise DatabaseIdentityError("IDENTITY_CHANGED", f"the path now resolves to {walked}")
        try:
            info = os.stat(self.identity.path, follow_symlinks=False)
        except FileNotFoundError:
            raise DatabaseIdentityError("IDENTITY_CHANGED", "database disappeared") from None
        if not self.identity.matches(info):
            raise DatabaseIdentityError("IDENTITY_CHANGED", "the database path names another file")
        check_file_security(self._anchor_fd, f"database {self.identity.path}")
        try:
            named = os.lstat(self._lock_path)
        except FileNotFoundError:
            raise DatabaseIdentityError("LOCK_IDENTITY_CHANGED", "lock file vanished") from None
        locked = os.fstat(self._lock_fd)
        if (named.st_dev, named.st_ino) != (locked.st_dev, locked.st_ino):
            raise DatabaseIdentityError("LOCK_IDENTITY_CHANGED", "lock file was replaced")
        check_file_security(self._lock_fd, f"lock {self._lock_path}")

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
