"""Integration I1m: trusted database directory boundary, and an honest double-swap reproduction.

Identity checks see a replacement only while it is observable; a swap-and-restore completed
between two checks can evade them (reproduced below, and documented as such). The supported
deployment therefore refuses database locations an untrusted principal could manipulate:
`ownership.resolve_trusted` (I1m's `check_trusted_location`, completed in I1o) runs before ownership or offline revocation, and the database
directory is re-checked at every identity checkpoint.

Limitation: this machine offers no second OS account. Foreign ownership is modeled through the
`ownership._effective_uid` seam and is labeled as simulated; it is not cross-user adversarial
verification. ACL cases use real macOS ACL entries (`chmod +a`) and are skipped elsewhere.
Offline: fake providers; no network, model or XRPL.
"""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from i1_world import GENERAL_FACILITY, agent_signer, job, market_world, open_service
from inference_world import AGENT_KEY

from credence.core.settlement_coordinator import SettlementCoordinator
from credence.core.signatures import key_id_for
from credence.integration import ownership as ownership_module
from credence.integration.journal import OperationState as S
from credence.integration.ownership import DatabaseIdentityError
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
def restore_modes(tmp_path: Path) -> Iterator[list[Path]]:
    """Directories whose mode or ACL a test changes; restored so tmp cleanup works."""
    changed: list[Path] = []
    yield changed
    for path in changed:
        if sys.platform == "darwin":
            subprocess.run(["chmod", "-N", str(path)], check=False)
        os.chmod(path, 0o700)


def refusal(call: Callable[[], object]) -> str:
    with pytest.raises((DatabaseIdentityError, OfflineRevocationRefused)) as exc:
        call()
    value = exc.value
    assert isinstance(value, (DatabaseIdentityError, OfflineRevocationRefused))
    return value.reason


def directory(parent: Path, name: str, mode: int) -> Path:
    path = parent / name
    path.mkdir()
    os.chmod(path, mode)
    return path


def existing_database(where: Path) -> Path:
    db = where / "db.sqlite3"
    open_service(db, market_world(), register=(GENERAL_FACILITY,)).close()
    return db


def opens(db: Path) -> bool:
    open_service(db, market_world()).close()
    return True


# --- accepted configurations ---------------------------------------------------------------------


@pytest.mark.parametrize("mode", [0o700, 0o750, 0o755])
def test_private_owner_controlled_directories_are_accepted(tmp_path: Path, mode: int) -> None:
    where = directory(tmp_path, "private", mode)
    db = existing_database(where)
    assert opens(db)
    assert revoke_key(db, A, "ops").key_id == A


def test_a_sticky_world_writable_ancestor_with_our_own_child_is_accepted(
    tmp_path: Path, restore_modes: list[Path]
) -> None:
    shared = directory(tmp_path, "shared", 0o1777)  # like /tmp
    restore_modes.append(shared)
    where = directory(shared, "mine", 0o700)  # ours: others cannot rename or delete it
    assert opens(existing_database(where))


def test_a_directory_symlink_kept_in_a_private_directory_is_accepted(tmp_path: Path) -> None:
    where = directory(tmp_path, "real", 0o700)
    db = existing_database(where)
    link = tmp_path / "linked"
    link.symlink_to(where, target_is_directory=True)
    assert opens(link / "db.sqlite3") and opens(db)
    assert revoke_key(link / "db.sqlite3", A, "ops").key_id == A


@darwin_only
def test_a_deny_only_acl_is_accepted(tmp_path: Path, restore_modes: list[Path]) -> None:
    where = directory(tmp_path, "denied", 0o700)
    subprocess.run(["chmod", "+a", "everyone deny delete", str(where)], check=True)
    restore_modes.append(where)
    assert opens(existing_database(where))


# --- refused configurations ----------------------------------------------------------------------


@pytest.mark.parametrize("mode", [0o770, 0o775, 0o777, 0o1777, 0o1775])
def test_a_group_or_world_writable_database_directory_is_refused(
    tmp_path: Path, mode: int, restore_modes: list[Path]
) -> None:
    where = directory(tmp_path, "writable", 0o700)
    db = existing_database(where)
    os.chmod(where, mode)
    restore_modes.append(where)
    assert refusal(lambda: open_service(db, market_world())) == "WRITABLE_DATABASE_DIRECTORY"
    assert refusal(lambda: revoke_key(db, A, "ops")) == "WRITABLE_DATABASE_DIRECTORY"
    fresh = where / "fresh.sqlite3"
    assert refusal(lambda: open_service(fresh, market_world())) == "WRITABLE_DATABASE_DIRECTORY"
    assert not fresh.exists() and not (where / "fresh.sqlite3.owner").exists()


@pytest.mark.parametrize("mode", [0o777, 0o775, 0o770])
def test_a_non_sticky_writable_ancestor_is_refused(
    tmp_path: Path, mode: int, restore_modes: list[Path]
) -> None:
    shared = directory(tmp_path, "shared", 0o700)
    where = directory(shared, "mine", 0o700)
    db = existing_database(where)
    os.chmod(shared, mode)  # anyone in it could rename "mine" away and put another in its place
    restore_modes.append(shared)
    assert refusal(lambda: open_service(db, market_world())) == "WRITABLE_DIRECTORY"
    assert refusal(lambda: revoke_key(db, A, "ops")) == "WRITABLE_DIRECTORY"


def test_a_symlink_kept_in_a_writable_directory_is_refused(
    tmp_path: Path, restore_modes: list[Path]
) -> None:
    where = directory(tmp_path, "real", 0o700)
    db = existing_database(where)
    drop = directory(tmp_path, "drop", 0o777)  # someone else could retarget links here
    restore_modes.append(drop)
    (drop / "linked").symlink_to(where, target_is_directory=True)
    (drop / "current.sqlite3").symlink_to(db)
    for alias in (drop / "linked" / "db.sqlite3", drop / "current.sqlite3"):
        assert refusal(lambda a=alias: open_service(a, market_world())) == "WRITABLE_DIRECTORY"  # type: ignore[misc]
        assert refusal(lambda a=alias: revoke_key(a, A, "ops")) == "WRITABLE_DIRECTORY"  # type: ignore[misc]
    assert opens(db)  # the real path stays usable


@darwin_only
@pytest.mark.parametrize("on", ["database directory", "ancestor"])
def test_an_acl_granting_write_is_refused_whatever_the_mode_bits(
    tmp_path: Path, on: str, restore_modes: list[Path]
) -> None:
    ancestor = directory(tmp_path, "ancestor", 0o700)
    where = directory(ancestor, "db-dir", 0o700)
    db = existing_database(where)
    target = where if on == "database directory" else ancestor
    subprocess.run(["chmod", "+a", "everyone allow add_file,delete_child", str(target)], check=True)
    restore_modes.append(target)
    assert oct(os.stat(target).st_mode & 0o777) == "0o700"  # the mode bits look private
    assert refusal(lambda: open_service(db, market_world())) == "DIRECTORY_ACL"
    assert refusal(lambda: revoke_key(db, A, "ops")) == "DIRECTORY_ACL"


def test_a_directory_owned_by_another_user_is_refused_simulated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIMULATED: the trusted owner is modeled as another uid, so our directories look foreign."""
    where = directory(tmp_path, "theirs", 0o700)
    db = existing_database(where)
    monkeypatch.setattr(ownership_module, "_effective_uid", lambda: os.geteuid() + 4242)
    assert refusal(lambda: open_service(db, market_world())) == "UNTRUSTED_DIRECTORY_OWNER"
    assert refusal(lambda: revoke_key(db, A, "ops")) == "UNTRUSTED_DIRECTORY_OWNER"


def test_a_directory_made_writable_while_the_service_runs_poisons_it(
    tmp_path: Path, restore_modes: list[Path]
) -> None:
    where = directory(tmp_path, "private", 0o700)
    db = existing_database(where)
    world = market_world()
    service = open_service(db, world)
    os.chmod(where, 0o777)
    restore_modes.append(where)
    assert refusal(lambda: service.purchase(job(2), agent_signer)) == "WRITABLE_DATABASE_DIRECTORY"
    os.chmod(where, 0o700)
    assert refusal(lambda: service.purchase(job(3), agent_signer)) == "WRITABLE_DATABASE_DIRECTORY"
    assert world.nebius_transport.posts == []  # poisoned: refused until closed
    service.close()
    assert opens(db)


# --- I1l: replace-and-restore between checks ------------------------------------------------------


def test_i1l_a_swap_and_restore_between_checks_can_evade_identity_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces the I1l class honestly, by a principal with this user's own filesystem
    authority (the only principal the trusted-directory boundary admits). The swap happens after
    one checkpoint and is undone before the next, so no identity check observes it: Core's
    reservation lands in the substitute file. This is NOT prevented; the contract says so. The
    supported boundary is that no other principal can do this (see the refusal tests above)."""
    where = directory(tmp_path, "private", 0o700)
    db = existing_database(where)
    substitute = where / "substitute.sqlite3"
    aside = where / "aside.sqlite3"
    with sqlite3.connect(db) as source, sqlite3.connect(substitute) as copy:
        source.backup(copy)  # a valid copy: Core's write succeeds there
    os.chmod(substitute, 0o600)
    original_reserve = SettlementCoordinator.reserve

    def swapped_reserve(self: SettlementCoordinator, *args: object, **kwargs: object) -> object:
        os.rename(db, aside)
        os.rename(substitute, db)
        try:
            return original_reserve(self, *args, **kwargs)  # type: ignore[arg-type]
        finally:
            os.rename(db, substitute)
            os.rename(aside, db)

    monkeypatch.setattr(SettlementCoordinator, "reserve", swapped_reserve)
    service = open_service(db, market_world())
    evaded = True
    try:
        service.purchase(job(1), agent_signer)
    except DatabaseIdentityError:
        evaded = False  # would mean a checkpoint observed the swap
    except Exception:  # the purchase then fails downstream in Core; that is not detection
        pass
    service.close()
    with sqlite3.connect(db) as real, sqlite3.connect(substitute) as other:
        in_real = real.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
        in_other = other.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
    assert evaded, "a checkpoint observed the swap; update the contract if this changes"
    assert (in_real, in_other) == (0, 1)  # the reservation was written to the substitute


def test_i1l_a_swap_still_observable_at_a_checkpoint_is_detected(tmp_path: Path) -> None:
    where = directory(tmp_path, "private", 0o700)
    db = existing_database(where)
    world = market_world()
    service = open_service(db, world)
    substitute = where / "substitute.sqlite3"
    shutil.copyfile(db, substitute)
    os.replace(substitute, db)  # not restored before the next check
    assert refusal(lambda: service.purchase(job(1), agent_signer)) == "IDENTITY_CHANGED"
    assert world.nebius_transport.posts == []
    service.close()


def test_normal_operation_in_a_private_directory_is_unchanged(tmp_path: Path) -> None:
    where = directory(tmp_path, "private", 0o700)
    service = open_service(where / "db.sqlite3", market_world(), register=(GENERAL_FACILITY,))
    report = service.purchase(job(1), agent_signer)
    assert isinstance(report, PurchaseReport) and report.state is S.SETTLED
    service.close()
