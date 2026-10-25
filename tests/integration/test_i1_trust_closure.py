"""Integration I1o: nested symlink traversal (V1) and file ACL enforcement (V2), plus operational
revalidation of the trusted chain.

V1  Skopje's chain `private/chain -> shared/link -> private/data/db.sqlite3`, with `shared`
    world-writable and not sticky, passed I1m because only the caller's own spelling and the
    final canonical chain were checked. `resolve_trusted` now walks every component actually
    traversed, on every hop.
V2  A 0600 database or lock file with a macOS ACL granting `everyone` write or delete passed I1m.
    Both files are now checked through their open descriptors (ownership, link count, mode bits
    and ACLs) at startup, offline revocation and every identity checkpoint.

Limitation: no second OS account is available. Permission inspection is verified with real
modes and real macOS ACLs; actual cross-user exploitation is not executed. ACL cases are skipped
off macOS. Offline: fake providers; no network, model or XRPL.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from i1_world import GENERAL_FACILITY, agent_signer, job, market_world, open_service
from inference_world import AGENT_KEY

from credence.core.signatures import key_id_for
from credence.integration.journal import OperationState as S
from credence.integration.ownership import DatabaseIdentityError, ProcessOwnership
from credence.integration.revocation import OfflineRevocationRefused, revoke_key
from credence.integration.service import PurchaseReport

A = key_id_for(AGENT_KEY.verify_key)
darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS ACLs (chmod +a)")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


@pytest.fixture
def restore(tmp_path: Path) -> Iterator[list[tuple[Path, int]]]:
    """(path, mode) to restore, with ACLs cleared, so tmp cleanup works."""
    changed: list[tuple[Path, int]] = []
    yield changed
    for path, mode in changed:
        if sys.platform == "darwin":
            subprocess.run(["chmod", "-N", str(path)], check=False)
        if path.exists():
            os.chmod(path, mode)


def acl(path: Path, entry: str, restore: list[tuple[Path, int]]) -> None:
    restore.append((path, os.stat(path).st_mode & 0o7777))
    subprocess.run(["chmod", "+a", entry, str(path)], check=True)


def refusal(call: Callable[[], object]) -> str:
    with pytest.raises((DatabaseIdentityError, OfflineRevocationRefused)) as exc:
        call()
    value = exc.value
    assert isinstance(value, (DatabaseIdentityError, OfflineRevocationRefused))
    return value.reason


def snapshot(db: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        names = [
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1")
        ]
        return {n: sorted(conn.execute(f'SELECT * FROM "{n}"').fetchall()) for n in names}


def directory(parent: Path, name: str, mode: int) -> Path:
    path = parent / name
    path.mkdir()
    os.chmod(path, mode)
    return path


def private_database(tmp_path: Path) -> tuple[Path, Path]:
    """(private root, database at private/data/db.sqlite3) with one settled purchase."""
    private = directory(tmp_path, "private", 0o700)
    data = directory(private, "data", 0o700)
    db = data / "db.sqlite3"
    service = open_service(db, market_world(), register=(GENERAL_FACILITY,))
    report = service.purchase(job(1), agent_signer)
    assert isinstance(report, PurchaseReport) and report.state is S.SETTLED
    service.close()
    return private, db


def opens(db: Path) -> bool:
    open_service(db, market_world()).close()
    return True


# --- V1: nested symlink traversal ----------------------------------------------------------------


def test_v1_skopje_chain_through_a_writable_directory_is_refused(
    tmp_path: Path, restore: list[tuple[Path, int]]
) -> None:
    private, db = private_database(tmp_path)
    shared = directory(tmp_path, "shared", 0o700)
    (shared / "link").symlink_to(db)  # shared/link -> private/data/db.sqlite3
    (private / "chain").symlink_to(shared / "link")  # private/chain -> shared/link
    os.chmod(shared, 0o777)  # world-writable, not sticky: anyone could retarget shared/link
    restore.append((shared, 0o700))
    before = snapshot(db)
    chain = private / "chain"
    assert refusal(lambda: open_service(chain, market_world())) == "WRITABLE_DIRECTORY"
    assert refusal(lambda: revoke_key(chain, A, "ops")) == "WRITABLE_DIRECTORY"
    assert snapshot(db) == before
    assert opens(db)  # the real path stays usable


def test_v1_nested_directory_symlinks_through_a_writable_directory_are_refused(
    tmp_path: Path, restore: list[tuple[Path, int]]
) -> None:
    private, db = private_database(tmp_path)
    shared = directory(tmp_path, "shared", 0o700)
    (shared / "hop").symlink_to(private / "data", target_is_directory=True)
    (private / "via").symlink_to(shared / "hop", target_is_directory=True)
    os.chmod(shared, 0o777)
    restore.append((shared, 0o700))
    assert refusal(lambda: open_service(private / "via" / "db.sqlite3", market_world())) == (
        "WRITABLE_DIRECTORY"
    )


def test_v1_multi_hop_chains_entirely_inside_private_directories_are_accepted(
    tmp_path: Path,
) -> None:
    private, db = private_database(tmp_path)
    (private / "c").symlink_to(db)
    (private / "b").symlink_to("c")  # relative
    (private / "a").symlink_to(private / "b")  # absolute
    (private / "up").symlink_to("data/../data/db.sqlite3")  # dot-dot inside the private tree
    (private / "dirlink").symlink_to("data", target_is_directory=True)
    for alias in (private / "a", private / "up", private / "dirlink" / "db.sqlite3"):
        assert opens(alias)
    assert revoke_key(private / "a", A, "ops").key_id == A


def test_v1_a_sticky_shared_directory_holding_our_own_links_is_accepted(
    tmp_path: Path, restore: list[tuple[Path, int]]
) -> None:
    private, db = private_database(tmp_path)
    shared = directory(tmp_path, "shared", 0o700)
    (shared / "link").symlink_to(db)
    (private / "chain").symlink_to(shared / "link")
    os.chmod(shared, 0o1777)  # like /tmp: others cannot replace links they do not own
    restore.append((shared, 0o700))
    assert opens(private / "chain")


def test_v1_symlink_loops_and_dangling_chains_are_refused(tmp_path: Path) -> None:
    private, _ = private_database(tmp_path)
    (private / "loop1").symlink_to(private / "loop2")
    (private / "loop2").symlink_to(private / "loop1")
    assert refusal(lambda: open_service(private / "loop1", market_world())) == "SYMLINK_LOOP"
    (private / "end").symlink_to(private / "data" / "missing.sqlite3")
    (private / "start").symlink_to(private / "end")
    assert refusal(lambda: open_service(private / "start", market_world())) == "DANGLING_SYMLINK"
    assert refusal(lambda: revoke_key(private / "start", A, "ops")) == "DANGLING_SYMLINK"
    assert not (private / "data" / "missing.sqlite3").exists()


def test_v1_a_link_retargeted_while_being_resolved_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private, db = private_database(tmp_path)
    other = private / "data" / "other.sqlite3"
    open_service(other, market_world()).close()
    link = private / "chain"
    link.symlink_to(db)
    real_readlink = os.readlink
    fired = {"done": False}

    def readlink_then_retarget(path: str | os.PathLike[str], *args: Any, **kwargs: Any) -> str:
        target = real_readlink(path, *args, **kwargs)
        if not fired["done"] and Path(str(path)) == link:
            fired["done"] = True
            link.unlink()
            link.symlink_to(other)  # retargeted between reading and validating
        return str(target)

    monkeypatch.setattr(os, "readlink", readlink_then_retarget)  # ownership uses os.readlink
    assert refusal(lambda: open_service(link, market_world())) == "PATH_CHANGED"
    monkeypatch.undo()
    ProcessOwnership.acquire(db).release()  # nothing left owned
    ProcessOwnership.acquire(other).release()


# --- V2: database and lock file ACLs and permissions ---------------------------------------------


@darwin_only
def test_v2_a_0600_database_with_an_everyone_write_delete_acl_is_refused(
    tmp_path: Path, restore: list[tuple[Path, int]]
) -> None:
    _, db = private_database(tmp_path)
    assert os.stat(db).st_mode & 0o777 == 0o600
    acl(db, "everyone allow write,delete,writesecurity", restore)
    before = snapshot(db)
    assert refusal(lambda: open_service(db, market_world())) == "FILE_ACL"
    assert refusal(lambda: revoke_key(db, A, "ops")) == "FILE_ACL"
    assert snapshot(db) == before  # no revocation, no accounting change


@darwin_only
def test_v2_a_0600_lock_file_with_an_everyone_delete_acl_is_refused(
    tmp_path: Path, restore: list[tuple[Path, int]]
) -> None:
    _, db = private_database(tmp_path)
    lock = db.with_name(db.name + ".owner")
    assert os.stat(lock).st_mode & 0o777 == 0o600
    acl(lock, "everyone allow write,delete", restore)
    before = snapshot(db)
    assert refusal(lambda: open_service(db, market_world())) == "LOCK_FILE_UNSAFE"
    assert refusal(lambda: revoke_key(db, A, "ops")) == "LOCK_FILE_UNSAFE"
    assert snapshot(db) == before


@darwin_only
@pytest.mark.parametrize(
    "entry", ["everyone allow read", "everyone deny delete,writesecurity,chown"]
)
def test_v2_read_only_and_deny_only_file_acls_are_accepted(
    tmp_path: Path, entry: str, restore: list[tuple[Path, int]]
) -> None:
    _, db = private_database(tmp_path)
    acl(db, entry, restore)
    acl(db.with_name(db.name + ".owner"), entry, restore)
    service = open_service(db, market_world())
    report = service.purchase(job(2), agent_signer)
    assert isinstance(report, PurchaseReport) and report.state is S.SETTLED
    service.close()
    assert revoke_key(db, A, "ops").key_id == A


@darwin_only
def test_v2_a_deny_acl_that_blocks_the_owner_fails_closed_with_a_typed_refusal(
    tmp_path: Path, restore: list[tuple[Path, int]]
) -> None:
    """`everyone deny write` also denies the owner: the lock cannot be opened for writing."""
    _, db = private_database(tmp_path)
    acl(db.with_name(db.name + ".owner"), "everyone deny write", restore)
    assert refusal(lambda: open_service(db, market_world())) == "LOCK_FILE_UNSAFE"


def test_v2_group_or_world_writable_database_file_is_refused_without_being_changed(
    tmp_path: Path,
) -> None:
    _, db = private_database(tmp_path)
    os.chmod(db, 0o620)
    assert refusal(lambda: open_service(db, market_world())) == "FILE_PERMISSIONS"
    assert os.stat(db).st_mode & 0o777 == 0o620  # refused, not "fixed" before being trusted
    os.chmod(db, 0o644)  # readable by others but not writable: accepted, then tightened
    assert opens(db) and os.stat(db).st_mode & 0o777 == 0o600


@darwin_only
def test_v2_an_acl_added_to_the_database_while_running_poisons_the_service(
    tmp_path: Path, restore: list[tuple[Path, int]]
) -> None:
    _, db = private_database(tmp_path)
    world = market_world()
    service = open_service(db, world)
    acl(db, "everyone allow write", restore)
    assert refusal(lambda: service.purchase(job(2), agent_signer)) == "FILE_ACL"
    assert world.nebius_transport.posts == []
    service.close()


def test_v2_a_lock_entry_deleted_and_recreated_while_running_poisons_the_owner(
    tmp_path: Path,
) -> None:
    """Under the supported boundary no other principal can delete the lock (private directory, no
    delete ACL: see above). A same-user deletion is outside it: a second process can then lock a
    new entry, and the original owner fails closed at its next checkpoint."""
    _, db = private_database(tmp_path)
    world = market_world()
    first = open_service(db, world)
    db.with_name(db.name + ".owner").unlink()
    second = ProcessOwnership.acquire(db)  # outside the boundary: same-user authority
    assert refusal(lambda: first.purchase(job(2), agent_signer)) == "LOCK_IDENTITY_CHANGED"
    assert world.nebius_transport.posts == []
    second.release()
    first.close()


# --- revalidation during operations --------------------------------------------------------------


def test_an_ancestor_made_writable_after_startup_poisons_the_service(
    tmp_path: Path, restore: list[tuple[Path, int]]
) -> None:
    private, db = private_database(tmp_path)
    world = market_world()
    service = open_service(db, world)
    os.chmod(private, 0o777)  # an ancestor of the database directory, not sticky
    restore.append((private, 0o700))
    assert refusal(lambda: service.purchase(job(2), agent_signer)) == "WRITABLE_DIRECTORY"
    os.chmod(private, 0o700)
    assert refusal(lambda: service.purchase(job(3), agent_signer)) == "WRITABLE_DIRECTORY"
    assert world.nebius_transport.posts == []
    service.close()


@darwin_only
def test_an_ancestor_acl_added_after_startup_poisons_the_service(
    tmp_path: Path, restore: list[tuple[Path, int]]
) -> None:
    private, db = private_database(tmp_path)
    service = open_service(db, market_world())
    acl(private, "everyone allow add_file,delete_child", restore)
    assert refusal(lambda: service.purchase(job(2), agent_signer)) == "DIRECTORY_ACL"
    service.close()
