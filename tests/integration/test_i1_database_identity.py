"""Integration I1k: ownership and offline revocation bound to the canonical database identity.

Skopje's I1j exploit: a symlink alias derived a different `.owner` lock, so the offline revocation
committed while the service ran (and the service ignored it), and a second service opened the same
database. Here every path goes through `ProcessOwnership.acquire`: canonical path, owner lock from
it, device and inode verified against the opened file, one hard link, and re-verification at
checkpoints and inside the integration's write transactions.

A  symlink alias, second service       B  symlink alias, offline revocation
C  hard links                          D  identity and replacement races
E  fresh database through an alias

Deterministic races use the test-only seams `ownership._race_point` and
`revocation._offline_race_point`. Offline: fake providers; no network, model or XRPL.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from i1_world import (
    GENERAL_FACILITY,
    Clock,
    acknowledge,
    agent_signer,
    job,
    market_world,
    open_service,
    retrieve,
)
from inference_world import AGENT_KEY, GATEWAY_KEY

from credence.core.signatures import key_id_for
from credence.integration import ownership as ownership_module
from credence.integration import revocation as revocation_module
from credence.integration.authorization import NotAuthorized
from credence.integration.journal import OperationState as S
from credence.integration.ownership import DatabaseIdentityError, OwnershipError, ProcessOwnership
from credence.integration.revocation import OfflineRevocationRefused, revoke_key
from credence.integration.service import IntegrationService, PurchaseReport

A = key_id_for(AGENT_KEY.verify_key)
G = key_id_for(GATEWAY_KEY.verify_key)
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def snapshot(db: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        names = [
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1")
        ]
        return {n: sorted(conn.execute(f'SELECT * FROM "{n}"').fetchall()) for n in names}


def revocation_rows(db: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        return [
            (str(r[0]), str(r[1]))
            for r in conn.execute("SELECT key_id, via FROM integration_key_revocations")
        ]


def settled_database(tmp_path: Path) -> tuple[Path, str]:
    """An existing integration database with one settled purchase; no service running."""
    db = tmp_path / "db.sqlite3"
    service = open_service(db, market_world(), register=(GENERAL_FACILITY,))
    report = service.purchase(job(1), agent_signer)
    assert isinstance(report, PurchaseReport) and report.state is S.SETTLED
    service.close()
    return db, report.operation_id


def other_integration_database(tmp_path: Path) -> Path:
    other = tmp_path / "other.sqlite3"
    open_service(other, market_world()).close()
    return other


def identity_refusal(call: Callable[[], object]) -> str:
    with pytest.raises((DatabaseIdentityError, OfflineRevocationRefused)) as exc:
        call()
    refusal = exc.value
    assert isinstance(refusal, (DatabaseIdentityError, OfflineRevocationRefused))
    return refusal.reason


def cli(
    database: Path, key: str, operator: str = "incident-response"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "credence.integration.revocation", "--database",
         str(database), "--key-id", key, "--operator", operator],
        capture_output=True, text=True, cwd=REPO, timeout=60,
    )  # fmt: skip


# --- A. symlink alias: second service ------------------------------------------------------------


@pytest.mark.parametrize("first_via_alias", [False, True])
def test_a_second_service_through_another_spelling_is_refused(
    tmp_path: Path, first_via_alias: bool
) -> None:
    db, _ = settled_database(tmp_path)
    alias = tmp_path / "current.sqlite3"
    alias.symlink_to(db)
    first_path, second_path = (alias, db) if first_via_alias else (db, alias)
    first = open_service(first_path, market_world())
    assert first.database == db.resolve(strict=True)
    before = snapshot(db)
    second_world = market_world()
    with pytest.raises(OwnershipError):
        open_service(second_path, second_world)
    assert second_world.nebius_transport.posts == [] and second_world.local.calls == 0
    assert snapshot(db) == before  # the refused owner changed nothing
    first.close()
    reopened = open_service(second_path, market_world())  # normal reopening after shutdown
    assert reopened.database == db.resolve(strict=True)
    reopened.close()


# --- B. symlink alias: offline revocation --------------------------------------------------------


def test_b_offline_revocation_through_an_alias_is_refused_while_the_service_runs(
    tmp_path: Path,
) -> None:
    db, _ = settled_database(tmp_path)
    alias = tmp_path / "current.sqlite3"
    alias.symlink_to(db)
    service = open_service(db, market_world())
    with pytest.raises(OwnershipError):
        revoke_key(alias, G, "incident-response")
    result = cli(alias, G.value)
    assert result.returncode == 2
    assert "REFUSED: OWNED_BY_RUNNING_SERVICE" in result.stderr and "Traceback" not in result.stderr
    assert revocation_rows(db) == []  # no committed revocation, no apparent success
    service.close()


def test_b_offline_revocation_through_an_alias_after_shutdown_holds_through_either_path(
    tmp_path: Path,
) -> None:
    db, op = settled_database(tmp_path)
    alias = tmp_path / "current.sqlite3"
    alias.symlink_to(db)
    record = revoke_key(alias, A, "incident-response")
    assert record.key_id == A and record.via == "offline_admin"
    assert revocation_rows(db) == [(A.value, "offline_admin")]  # the intended database
    for path in (db, alias):
        world = market_world()
        service = open_service(path, world, clock=Clock())
        with pytest.raises(NotAuthorized) as refused:
            retrieve(service, op)
        assert refused.value.reason == "REVOKED_KEY"
        with pytest.raises(NotAuthorized):
            acknowledge(service, op)
        purchase = service.purchase(job(2), agent_signer)
        assert isinstance(purchase, PurchaseReport) and purchase.detail == "KEY_REVOKED"
        assert world.nebius_transport.posts == []
        service.close()


def test_b_revoked_gateway_key_is_not_accepted_for_new_settlement(tmp_path: Path) -> None:
    db, _ = settled_database(tmp_path)
    alias = tmp_path / "current.sqlite3"
    alias.symlink_to(db)
    result = cli(alias, G.value)
    assert result.returncode == 0, result.stderr
    world = market_world()
    service = open_service(alias, world)
    report = service.purchase(job(2), agent_signer)
    assert isinstance(report, PurchaseReport)
    assert (report.state, report.reason_code) == (S.DISPUTED, "RECEIPT_SIG_INVALID")
    calls = len(world.nebius_transport.posts)
    service.recover()
    assert len(world.nebius_transport.posts) == calls  # no automatic re-execution
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounting_settlements").fetchone() == (1,)
    service.close()


# --- C. hard links -------------------------------------------------------------------------------


def _hard_link(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError as exc:  # documented limitation, not a simulated pass
        pytest.skip(f"this filesystem cannot create hard links: {exc.strerror}")


def test_c_a_hard_linked_database_is_refused_through_every_path(tmp_path: Path) -> None:
    db, _ = settled_database(tmp_path)
    revoke_key(db, key_id_for(type(AGENT_KEY)(b"\x09" * 32).verify_key), "ops")  # a prior record
    hard = tmp_path / "hard.sqlite3"
    _hard_link(db, hard)
    before = snapshot(db)
    for path in (db, hard):
        _assert_hard_link_refused(path)
    assert snapshot(db) == before  # nothing changed; the prior revocation is kept
    hard.unlink()
    open_service(db, market_world()).close()  # one link again: accepted


def _assert_hard_link_refused(path: Path) -> None:
    assert identity_refusal(lambda: open_service(path, market_world())) == "HARD_LINKED"
    assert identity_refusal(lambda: revoke_key(path, A, "ops")) == "HARD_LINKED"
    result = cli(path, A.value)
    assert result.returncode == 2 and "REFUSED: HARD_LINKED" in result.stderr


def test_c_a_hard_link_made_after_the_service_opened_poisons_it(tmp_path: Path) -> None:
    db, op = settled_database(tmp_path)
    world = market_world()
    service = open_service(db, world)
    before = snapshot(db)
    hard = tmp_path / "hard.sqlite3"
    _hard_link(db, hard)
    assert identity_refusal(lambda: service.purchase(job(2), agent_signer)) == "HARD_LINKED"
    assert identity_refusal(lambda: retrieve(service, op)) == "HARD_LINKED"  # stays refused
    assert identity_refusal(lambda: revoke_key(hard, A, "ops")) == "HARD_LINKED"
    assert world.nebius_transport.posts == [] and snapshot(db) == before
    service.close()
    hard.unlink()
    open_service(db, market_world()).close()


# --- D. identity and replacement races -----------------------------------------------------------


@pytest.fixture
def race(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, Callable[[], None]], None]:
    def arm(point: str, action: Callable[[], None]) -> None:
        fired = {"done": False}

        def hook(reached: str) -> None:
            if reached == point and not fired["done"]:
                fired["done"] = True
                action()

        monkeypatch.setattr(ownership_module, "_race_point", hook)
        monkeypatch.setattr(revocation_module, "_offline_race_point", hook)

    return arm


def _free(db: Path) -> None:
    ProcessOwnership.acquire(db).release()  # nobody still owns it


def test_d_alias_retargeted_between_resolution_and_open_is_refused(
    tmp_path: Path, race: Callable[[str, Callable[[], None]], None]
) -> None:
    db, _ = settled_database(tmp_path)
    other = other_integration_database(tmp_path)
    alias = tmp_path / "current.sqlite3"
    alias.symlink_to(db)

    def retarget() -> None:
        alias.unlink()
        alias.symlink_to(other)

    race("after_resolve", retarget)
    assert identity_refusal(lambda: open_service(alias, market_world())) == "ALIAS_CHANGED"
    _free(db)
    _free(other)


def test_d_database_replaced_after_inspection_is_refused(
    tmp_path: Path, race: Callable[[str, Callable[[], None]], None]
) -> None:
    db, _ = settled_database(tmp_path)
    other = other_integration_database(tmp_path)
    race("after_resolve", lambda: os.replace(other, db))
    assert identity_refusal(lambda: open_service(db, market_world())) == "IDENTITY_CHANGED"
    _free(db)


def test_d_delete_and_recreate_under_the_same_name_is_refused(
    tmp_path: Path, race: Callable[[str, Callable[[], None]], None]
) -> None:
    db, _ = settled_database(tmp_path)
    replacement = tmp_path / "replacement.sqlite3"
    sqlite3.connect(replacement).close()

    def recreate() -> None:
        db.unlink()
        os.rename(replacement, db)  # a distinct inode now has the original name

    race("after_resolve", recreate)
    assert identity_refusal(lambda: open_service(db, market_world())) == "IDENTITY_CHANGED"


def test_d_an_existing_database_that_disappears_is_never_silently_recreated(
    tmp_path: Path, race: Callable[[str, Callable[[], None]], None]
) -> None:
    db, _ = settled_database(tmp_path)
    race("after_resolve", db.unlink)
    assert identity_refusal(lambda: open_service(db, market_world())) == "IDENTITY_CHANGED"
    assert not db.exists()


def test_d_alias_retargeted_during_offline_revocation_commits_nowhere(
    tmp_path: Path, race: Callable[[str, Callable[[], None]], None]
) -> None:
    db, _ = settled_database(tmp_path)
    other = other_integration_database(tmp_path)
    alias = tmp_path / "current.sqlite3"
    alias.symlink_to(db)

    def retarget() -> None:
        alias.unlink()
        alias.symlink_to(other)

    race("after_lock", retarget)
    assert identity_refusal(lambda: revoke_key(alias, A, "ops")) == "ALIAS_CHANGED"
    assert revocation_rows(db) == [] and revocation_rows(other) == []


def test_d_database_replaced_during_offline_revocation_commits_nowhere(
    tmp_path: Path, race: Callable[[str, Callable[[], None]], None]
) -> None:
    db, _ = settled_database(tmp_path)
    other = other_integration_database(tmp_path)
    race("before_store", lambda: os.replace(other, db))
    assert identity_refusal(lambda: revoke_key(db, A, "ops")) == "IDENTITY_CHANGED"
    assert revocation_rows(db) == []  # the file now at that path received nothing


def test_d_database_replaced_while_the_service_runs_poisons_it(tmp_path: Path) -> None:
    db, op = settled_database(tmp_path)
    world = market_world()
    service = open_service(db, world)
    replacement = tmp_path / "replacement.sqlite3"
    with sqlite3.connect(replacement) as conn:
        conn.execute("CREATE TABLE marker (x INTEGER)")
    os.replace(replacement, db)
    assert identity_refusal(lambda: service.purchase(job(2), agent_signer)) == "IDENTITY_CHANGED"
    assert identity_refusal(lambda: retrieve(service, op)) == "IDENTITY_CHANGED"
    assert world.nebius_transport.posts == []
    assert set(snapshot(db)) == {"marker"}  # the replacement was not written to
    service.close()


def _acquire_race(tmp_path: Path) -> int:
    db, _ = settled_database(tmp_path)
    alias = tmp_path / "current.sqlite3"
    alias.symlink_to(db)
    barrier = threading.Barrier(2)
    owners: list[IntegrationService] = []
    refusals: list[str] = []

    def open_via(path: Path) -> None:
        barrier.wait()
        try:
            owners.append(open_service(path, market_world()))
        except (OwnershipError, DatabaseIdentityError) as exc:
            refusals.append(type(exc).__name__)

    threads = [threading.Thread(target=open_via, args=(p,)) for p in (db, alias)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    count = len(owners)
    for owner in owners:
        owner.close()
    return count


def test_d_concurrent_acquisition_through_real_and_alias_paths_has_one_owner(
    tmp_path: Path,
) -> None:
    for round_ in range(20):
        (tmp_path / str(round_)).mkdir()
        assert _acquire_race(tmp_path / str(round_)) == 1


# --- E. fresh database through an alias ----------------------------------------------------------


def test_e_fresh_database_through_a_symlinked_parent_has_one_canonical_identity(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    service = open_service(linked / "new.sqlite3", market_world(), register=(GENERAL_FACILITY,))
    assert service.database == (real / "new.sqlite3").resolve(strict=True)
    assert (real / "new.sqlite3.owner").exists() and not (real / "linked").exists()
    with pytest.raises(OwnershipError):
        open_service(real / "new.sqlite3", market_world())
    service.close()


def _fresh_race(tmp_path: Path) -> int:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    barrier = threading.Barrier(2)
    owners: list[IntegrationService] = []

    def open_via(path: Path) -> None:
        barrier.wait()
        try:
            owners.append(open_service(path, market_world()))
        except (OwnershipError, DatabaseIdentityError):
            pass

    threads = [
        threading.Thread(target=open_via, args=(p,))
        for p in (real / "new.sqlite3", linked / "new.sqlite3")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    count = len(owners)
    for owner in owners:
        owner.close()
    assert sorted(p.name for p in real.iterdir()) == ["new.sqlite3", "new.sqlite3.owner"]
    return count


def test_e_concurrent_fresh_initialization_through_aliases_has_one_owner(tmp_path: Path) -> None:
    for round_ in range(20):
        (tmp_path / str(round_)).mkdir()
        assert _fresh_race(tmp_path / str(round_)) == 1


def test_e_a_dangling_symlink_is_not_a_fresh_database_request(tmp_path: Path) -> None:
    target = tmp_path / "missing.sqlite3"
    alias = tmp_path / "current.sqlite3"
    alias.symlink_to(target)
    assert identity_refusal(lambda: open_service(alias, market_world())) == "DANGLING_SYMLINK"
    assert identity_refusal(lambda: revoke_key(alias, A, "ops")) == "DANGLING_SYMLINK"
    assert not target.exists()


def test_e_a_database_appearing_during_fresh_initialization_is_not_adopted(
    tmp_path: Path, race: Callable[[str, Callable[[], None]], None]
) -> None:
    db = tmp_path / "new.sqlite3"
    race("after_lock", lambda: sqlite3.connect(db).close())  # someone else creates it
    assert identity_refusal(lambda: open_service(db, market_world())) == "IDENTITY_CHANGED"


def test_e_a_missing_parent_is_refused_without_creating_anything(tmp_path: Path) -> None:
    db = tmp_path / "absent" / "new.sqlite3"
    assert identity_refusal(lambda: open_service(db, market_world())) == "PARENT_MISSING"
    assert not (tmp_path / "absent").exists()
