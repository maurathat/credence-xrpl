"""Durable, fail-closed key revocation for an integration database (Integration I1g, finding F1).

Core's C3A `KeyRegistry` is an in-memory object, and it is frozen. A revocation made on it used
to vanish when the service restarted with a freshly built registry. Here revocation is an
operational record in the shared SQLite database, authoritative over any registry supplied later:

- `integration_key_revocations` keeps the key identity, the revocation time and the trusted
  administrative actor (the operator identity the service was opened with). Rows never expire and
  are never deleted; registering the same key again does not restore it.
- `DurableKeyRegistry` is what the service hands to Core and the gateway. Its `lookup` reports a
  persisted-revoked key as `revoked` whatever the supplied registry says. It is a subclass used
  only inside the integration; C3A signature semantics and formats are unchanged.
- The existing workflow, `KeyRegistry.revoke(key_id)` on the registry the service was opened with,
  is routed through persistence while the service is open: the record is committed before the
  revocation takes effect. A revocation already present in the supplied registry when the service
  opens is persisted at open.
- Fail closed: an unreadable or malformed revocation record raises `RevocationStateError`, and the
  operation (or opening the service) is refused. If persisting a revocation fails, the key is still
  refused in this process and `RevocationNotPersisted` is raised.

There is no public revocation operation for agents or remote callers: revoking requires the
operator's in-process registry object.

Owner: Integration (San Antonio).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from credence.core.primitives import DomainError, KeyId, Timestamp
from credence.core.signatures import KeyRegistry, KeyStatus, RegisteredKey

SCHEMA = """
CREATE TABLE IF NOT EXISTS integration_key_revocations (
    key_id TEXT PRIMARY KEY,
    revoked_at TEXT NOT NULL,
    revoked_by TEXT NOT NULL,
    via TEXT NOT NULL CHECK (via IN ('key_registry_revoke'))
)
"""


class RevocationStateError(RuntimeError):
    """The persisted revocation state is unreadable or malformed. Fail closed."""


class RevocationNotPersisted(RuntimeError):
    """A revocation could not be persisted. It still applies in this process."""


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
        if via != "key_registry_revoke":
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

    def revoke(self, key_id: KeyId, now: Timestamp, actor: str) -> Revocation:
        """Persist a revocation (first one kept). Commits before returning."""
        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO integration_key_revocations (key_id, revoked_at, "
                "revoked_by, via) VALUES (?, ?, ?, 'key_registry_revoke')",
                (key_id.value, now.value, actor),
            )
            stored = revocation_in(conn, key_id.value)
        assert stored is not None
        return stored


class DurableKeyRegistry(KeyRegistry):
    """The registry the integration hands to Core and the gateway.

    Keys come from the operator's supplied registry; persisted revocations override it.

    `lookup` does no database I/O: Core calls it inside its own write transactions (settlement
    verifies signatures in-transaction), so it reads an in-memory mirror of the persisted
    revocations, loaded at `bind` and updated under the gate whenever a revocation commits. One
    process owns the database, so the mirror equals the database. Where ordering matters the
    database is read directly: the journal's access transactions re-read the revocation row, and
    purchase admission re-reads it under the gate (`persisted_revocation`)."""

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
        self._unpersisted: set[KeyId] = set()
        self._revoked: set[KeyId] = set()
        self._original_revoke: Callable[[KeyId], None] | None = None

    # --- routing the existing workflow -----------------------------------------------------

    def bind(self) -> None:
        """Route `supplied.revoke` through persistence, and persist revocations it already has."""
        self._revoked = set(self._store.all())
        existing = getattr(self._supplied, "_keys", {})
        for key_id, key in dict(existing).items():
            if key.status is KeyStatus.REVOKED:
                self._persist(key_id)
        self._original_revoke = self._supplied.revoke
        setattr(self._supplied, "revoke", self.revoke)  # noqa: B010 - an instance override

    def unbind(self) -> None:
        if self._original_revoke is not None:
            if "revoke" in vars(self._supplied):
                delattr(self._supplied, "revoke")
            self._original_revoke = None

    def _persist(self, key_id: KeyId) -> None:
        try:
            self._store.revoke(key_id, self._clock(), self._actor)
            self._revoked.add(key_id)
        except sqlite3.Error as exc:
            self._unpersisted.add(key_id)  # still refused in this process
            raise RevocationNotPersisted(
                f"{key_id.value}: revocation not persisted ({type(exc).__name__}); "
                "refused in this process only"
            ) from None

    # --- KeyRegistry ----------------------------------------------------------------------------

    def register(self, key: RegisteredKey) -> None:
        raise RuntimeError("register keys in the operator's registry, not the durable view")

    def revoke(self, key_id: KeyId) -> None:
        """Persist first (committed), then mark the supplied registry. Serialized with the
        integration's authorization commits through the shared gate."""
        with self._gate:
            self._persist(key_id)
            original = self._original_revoke or KeyRegistry.revoke.__get__(self._supplied)
            if self._supplied.lookup(key_id) is not None:
                original(key_id)

    def persisted_revocation(self, key_id: KeyId) -> bool:
        """Read the database directly (fail closed on bad state). Not for use inside another
        open write transaction on this database."""
        revoked = self._store.get(key_id) is not None
        if revoked:
            self._revoked.add(key_id)
        return revoked or key_id in self._unpersisted

    def lookup(self, key_id: KeyId) -> RegisteredKey | None:
        key = self._supplied.lookup(key_id)
        if key is None:
            return None
        if key_id in self._unpersisted or key_id in self._revoked:
            return RegisteredKey(key.key_id, key.role, key.subject, KeyStatus.REVOKED)
        if key.status is KeyStatus.REVOKED:
            with self._gate:
                self._persist(key_id)  # an in-memory revocation made outside the binding
        return key
