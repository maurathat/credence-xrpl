"""Durable, fail-closed key revocation for an integration database (I1g F1, closed in I1i).

Core's C3A `KeyRegistry` is an in-memory object, and it is frozen. Revocation that must survive
restarts is an operational record in the shared SQLite database, authoritative over any registry
supplied later. These are distinct:

- **Core in-memory revocation.** `KeyRegistry.revoke` on a registry that was never bound to an
  integration service only changes that object. It is not a durable Credence administrative
  operation.
- **Durable bound revocation.** While an `IntegrationService` is open, `revoke` on the registry it
  was opened with commits a record to `integration_key_revocations` before it returns, then marks
  the key revoked. Revocations already present in that registry are persisted when it is bound.
- **Revocation after the service closed.** The bound registry's `revoke` does not revert to Core's
  in-memory method: it marks the key revoked in that object (fail closed in-process) and raises
  `RevocationNotPersisted`, because nothing was committed. Closing twice keeps this; binding the
  same object to a new service restores durable behavior (and persists what it marked).
- **Trusted offline administrative revocation.** `revoke_key(database, key_id, operator)` commits a
  record with no service running. Trust is the operating system's: the caller must be the OS user
  owning the existing owner-only (0600) integration database and must hold its exclusive ownership
  lock. It never creates a database, is idempotent and irreversible, and reports success only
  after commit. Command line: `python -m credence.integration.revocation --database PATH
  --key-id ed25519:... --operator NAME`. It is not an HTTP endpoint.
- **Durable state on restart.** Persisted revocations override any registry supplied later, never
  expire, and survive re-registration of the same key.
- **Unexpected in-process revocation.** A key found revoked in the supplied registry but with no
  persisted record (for example through `KeyRegistry.revoke(registry, key_id)`, which bypasses the
  binding) is refused at once, in this process only. `lookup` does not persist it.

`DurableKeyRegistry.lookup` performs no database I/O on any branch: Core calls it inside its own
write transactions (settlement verifies signatures in-transaction). Fail closed: unreadable or
malformed revocation state raises `RevocationStateError`; a failed persistence raises
`RevocationNotPersisted` and the key stays refused in-process.

Owner: Integration (San Antonio).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import stat
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from credence.core.primitives import DomainError, KeyId, Timestamp
from credence.core.signatures import KeyRegistry, KeyStatus, RegisteredKey
from credence.integration.ownership import ProcessOwnership

VIAS = ("key_registry_revoke", "offline_admin")

SCHEMA = """
CREATE TABLE IF NOT EXISTS integration_key_revocations (
    key_id TEXT PRIMARY KEY,
    revoked_at TEXT NOT NULL,
    revoked_by TEXT NOT NULL,
    via TEXT NOT NULL CHECK (via IN ('key_registry_revoke', 'offline_admin'))
)
"""


class RevocationStateError(RuntimeError):
    """The persisted revocation state is unreadable or malformed. Fail closed."""


class RevocationNotPersisted(RuntimeError):
    """No durable revocation was committed. Where noted, the key is still refused in-process."""


class OfflineRevocationRefused(RuntimeError):
    """The offline administrative revocation refused to run. `reason` is a code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class RegistryAlreadyBound(RuntimeError):
    """The registry is bound to another open integration service."""


@dataclass(frozen=True)
class Revocation:
    key_id: KeyId
    revoked_at: Timestamp
    revoked_by: str
    via: str


def _parse(row: tuple[object, ...]) -> Revocation:
    try:
        key_id, revoked_at, revoked_by, via = row
        if not isinstance(revoked_by, str) or not revoked_by.strip():
            raise ValueError("revoked_by")
        if via not in VIAS:
            raise ValueError("via")
        return Revocation(KeyId(str(key_id)), Timestamp(str(revoked_at)), revoked_by, str(via))
    except (ValueError, TypeError, DomainError) as exc:
        raise RevocationStateError(f"malformed revocation record: {type(exc).__name__}") from None


def revocation_in(conn: sqlite3.Connection, key_id: str) -> Revocation | None:
    """The persisted revocation of `key_id`, read on `conn` (inside the caller's transaction)."""
    try:
        row = conn.execute(
            "SELECT key_id, revoked_at, revoked_by, via FROM integration_key_revocations "
            "WHERE key_id = ?",
            (key_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise RevocationStateError(f"revocation state unreadable: {type(exc).__name__}") from None
    return None if row is None else _parse(tuple(row))


class RevocationStore:
    def __init__(self, path: Path, *, busy_timeout: float = 30) -> None:
        self._path = str(path)
        self._busy_timeout = busy_timeout
        with self._write() as conn:
            existing = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'integration_key_revocations'"
            ).fetchone()
            if existing is not None and "'offline_admin'" not in str(existing[0]):
                # I1g schema: widen the `via` check in place, keeping every record (one txn).
                conn.execute(
                    "ALTER TABLE integration_key_revocations RENAME TO integration_key_revocations_i1g"
                )
                conn.execute(SCHEMA)
                conn.execute(
                    "INSERT INTO integration_key_revocations SELECT key_id, revoked_at, "
                    "revoked_by, via FROM integration_key_revocations_i1g"
                )
                conn.execute("DROP TABLE integration_key_revocations_i1g")
            else:
                conn.execute(SCHEMA)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=self._busy_timeout, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def all(self) -> dict[KeyId, Revocation]:
        """Every persisted revocation, validated. Raises RevocationStateError if any is bad."""
        try:
            with self._write() as conn:
                rows = conn.execute(
                    "SELECT key_id, revoked_at, revoked_by, via FROM integration_key_revocations"
                ).fetchall()
        except sqlite3.Error as exc:
            raise RevocationStateError(
                f"revocation state unreadable: {type(exc).__name__}"
            ) from None
        parsed = [_parse(tuple(r)) for r in rows]
        return {r.key_id: r for r in parsed}

    def get(self, key_id: KeyId) -> Revocation | None:
        with self._write() as conn:
            return revocation_in(conn, key_id.value)

    def revoke(
        self, key_id: KeyId, now: Timestamp, actor: str, *, via: str = "key_registry_revoke"
    ) -> Revocation:
        """Persist a revocation (first one kept: idempotent, irreversible). Commits first."""
        if via not in VIAS:
            raise ValueError(f"unknown revocation path {via!r}")
        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO integration_key_revocations (key_id, revoked_at, "
                "revoked_by, via) VALUES (?, ?, ?, ?)",
                (key_id.value, now.value, actor, via),
            )
            stored = revocation_in(conn, key_id.value)
        assert stored is not None
        return stored


_BINDING = "_credence_durable_binding"


class DurableKeyRegistry(KeyRegistry):
    """The registry the integration hands to Core and the gateway.

    Keys come from the operator's supplied registry; persisted revocations override it.

    `lookup` performs no database I/O on any branch. It reads an in-memory mirror of the persisted
    revocations (loaded at `bind`, updated under the gate whenever a revocation commits; one
    process owns the database, so the mirror equals the database) and the set of keys refused in
    this process. Where ordering matters the database is read directly, outside Core's
    transactions: the journal's access transactions re-read the revocation row, and purchase
    admission re-reads it under the gate (`persisted_revocation`)."""

    def __init__(
        self,
        supplied: KeyRegistry,
        store: RevocationStore,
        *,
        actor: str,
        clock: Callable[[], Timestamp],
        gate: threading.RLock,
    ) -> None:
        super().__init__(())
        if not actor.strip():
            raise ValueError("a revocation actor is required")
        self._supplied = supplied
        self._store = store
        self._actor = actor
        self._clock = clock
        self._gate = gate
        self._refused_in_process: set[KeyId] = set()
        self._revoked: set[KeyId] = set()
        self._bound = False

    # --- binding the operator's registry ---------------------------------------------------

    def bind(self) -> None:
        """Route `supplied.revoke` through persistence; persist revocations it already has."""
        holder = getattr(self._supplied, _BINDING, None)
        if isinstance(holder, DurableKeyRegistry) and holder is not self and holder._bound:
            raise RegistryAlreadyBound("this registry is bound to another open service")
        self._revoked = set(self._store.all())
        existing = getattr(self._supplied, "_keys", {})
        for key_id, key in dict(existing).items():
            if key.status is KeyStatus.REVOKED:
                with self._gate:
                    self._persist(key_id)
        setattr(self._supplied, _BINDING, self)  # noqa: B010 - instance-level marker
        setattr(self._supplied, "revoke", self.revoke)  # noqa: B010 - instance-level override
        self._bound = True

    def unbind(self) -> None:
        """On close: `revoke` on the operator's registry raises instead of silently reverting to
        Core's in-memory method. Idempotent."""
        if getattr(self._supplied, _BINDING, None) is not self:
            return  # a later service has bound this registry; leave its binding alone
        self._bound = False
        supplied = self._supplied

        def revoke_while_closed(key_id: KeyId) -> None:
            if supplied.lookup(key_id) is not None:
                KeyRegistry.revoke(supplied, key_id)  # fail closed in this object
            raise RevocationNotPersisted(
                f"{key_id.value}: no integration service is open, so no durable revocation was "
                "committed. The key is marked revoked in this registry object only. Use "
                "credence.integration.revocation.revoke_key for a durable offline revocation."
            )

        setattr(supplied, "revoke", revoke_while_closed)  # noqa: B010 - instance-level override

    def _persist(self, key_id: KeyId) -> None:
        """Commit a revocation record. Callers hold the gate and no other write transaction."""
        try:
            self._store.revoke(key_id, self._clock(), self._actor)
            self._revoked.add(key_id)
        except sqlite3.Error as exc:
            self._refused_in_process.add(key_id)  # still refused in this process
            raise RevocationNotPersisted(
                f"{key_id.value}: revocation not persisted ({type(exc).__name__}); "
                "refused in this process only"
            ) from None

    # --- KeyRegistry ----------------------------------------------------------------------------

    def register(self, key: RegisteredKey) -> None:
        raise RuntimeError("register keys in the operator's registry, not the durable view")

    def revoke(self, key_id: KeyId) -> None:
        """Bound revocation: commit the record first, then mark the key revoked. Serialized with
        the integration's purchase admission through the shared gate."""
        with self._gate:
            self._persist(key_id)
            if self._supplied.lookup(key_id) is not None:
                KeyRegistry.revoke(self._supplied, key_id)

    def persisted_revocation(self, key_id: KeyId) -> bool:
        """Read the database directly (fail closed on bad state). Never call this inside another
        open write transaction on this database."""
        revoked = self._store.get(key_id) is not None
        if revoked:
            self._revoked.add(key_id)
        return revoked or key_id in self._refused_in_process

    def lookup(self, key_id: KeyId) -> RegisteredKey | None:
        """No database I/O. A persisted, failed-to-persist or unexpected in-memory revocation all
        report the key as revoked; nothing is persisted here."""
        key = self._supplied.lookup(key_id)
        if key is None:
            return None
        if key.status is KeyStatus.REVOKED:
            self._refused_in_process.add(key_id)  # unexpected: refused here, not persisted
            return key
        if key_id in self._refused_in_process or key_id in self._revoked:
            return RegisteredKey(key.key_id, key.role, key.subject, KeyStatus.REVOKED)
        return key


# --- trusted offline administrative revocation ---------------------------------------------------


def _utc_now() -> Timestamp:
    return Timestamp(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


def revoke_key(
    database: Path,
    key_id: KeyId,
    operator: str,
    *,
    clock: Callable[[], Timestamp] = _utc_now,
) -> Revocation:
    """Durably revoke `key_id` with no integration service running. Success means committed.

    Trusted operator context, checked before anything is written:
    - the database must already exist (it is never created) and be an integration database;
    - it must be owned by the calling OS user and be owner-only (no group or other access);
    - the caller must take its exclusive ownership lock (refused while a service runs: use the
      open service's bound `registry.revoke` then).
    The record names the operator and the OS user. Idempotent: the first record is kept."""
    if not isinstance(key_id, KeyId):
        raise TypeError("key_id must be a KeyId")
    if not operator.strip():
        raise OfflineRevocationRefused("OPERATOR_REQUIRED")
    path = Path(database)
    try:
        info = os.stat(path)
    except FileNotFoundError:
        raise OfflineRevocationRefused("DATABASE_MISSING", str(path)) from None
    if not stat.S_ISREG(info.st_mode):
        raise OfflineRevocationRefused("NOT_A_FILE", str(path))
    if info.st_uid != os.getuid():
        raise OfflineRevocationRefused("NOT_OWNER", str(path))
    if info.st_mode & 0o077:
        raise OfflineRevocationRefused("INSECURE_PERMISSIONS", oct(stat.S_IMODE(info.st_mode)))
    ownership = ProcessOwnership.acquire(path)  # OwnershipError while a service runs
    try:
        try:
            probe = sqlite3.connect(f"file:{path}?mode=rw", uri=True)
            try:
                found = probe.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'integration_operations'"
                ).fetchone()
            finally:
                probe.close()
        except sqlite3.Error as exc:
            raise OfflineRevocationRefused("UNREADABLE_DATABASE", type(exc).__name__) from None
        if found is None:
            raise OfflineRevocationRefused("NOT_AN_INTEGRATION_DATABASE", str(path))
        actor = f"{operator.strip()} (os uid {os.getuid()})"
        try:
            store = RevocationStore(path)
            store.all()  # fail closed on malformed existing state
            return store.revoke(key_id, clock(), actor, via="offline_admin")
        except sqlite3.Error as exc:
            raise RevocationNotPersisted(
                f"{key_id.value}: offline revocation not committed ({type(exc).__name__})"
            ) from None
    finally:
        ownership.release()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m credence.integration.revocation",
        description="Durably revoke a key in an integration database (no service running).",
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--key-id", required=True, help="ed25519:<64 hex digits>")
    parser.add_argument("--operator", required=True, help="name recorded with the revocation")
    args = parser.parse_args(argv)
    try:
        record = revoke_key(args.database, KeyId(args.key_id), args.operator)
    except (OfflineRevocationRefused, RevocationNotPersisted, RevocationStateError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except DomainError as exc:
        print(f"REFUSED: malformed key id ({exc})", file=sys.stderr)
        return 2
    print(f"revoked {record.key_id.value} at {record.revoked_at.value} by {record.revoked_by}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
