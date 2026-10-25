"""Integration I1i: revocation lifecycle closure and no persistence inside `lookup`.

F1-I   Skopje's sequence: revoke after the service closed must not appear successful; an explicit
       offline administrative revocation persists and holds in a new process.
F1-II  Operator lifecycle: open, closed, offline, double close, rebinding, fresh registries,
       persistence failures, idempotence, unrelated agents, trusted-context refusals, races.
F1-III Nested-write regression: a class-method revocation that bypasses the binding fails closed
       promptly, with no database I/O inside `DurableKeyRegistry.lookup`.

Real signatures and registry; offline (fake providers); no network, model or XRPL.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from i1_world import (
    AGENT_B_KEY,
    GENERAL_FACILITY,
    Clock,
    acknowledge,
    agent_signer,
    job,
    market_world,
    open_service,
    registry_with_agent_b,
    retrieve,
)
from inference_world import AGENT_KEY, GATEWAY_KEY, NOW
from market_world import MarketWorld

from credence.core.signatures import KeyRegistry, KeyStatus, key_id_for
from credence.integration import revocation
from credence.integration.authorization import NotAuthorized
from credence.integration.journal import OperationState as S
from credence.integration.ownership import OwnershipError
from credence.integration.revocation import (
    DurableKeyRegistry,
    OfflineRevocationRefused,
    RegistryAlreadyBound,
    RevocationNotPersisted,
    RevocationStore,
    revoke_key,
)
from credence.integration.service import IntegrationService, PurchaseReport

A = key_id_for(AGENT_KEY.verify_key)
B = key_id_for(AGENT_B_KEY.verify_key)
G = key_id_for(GATEWAY_KEY.verify_key)
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OFFLINE_NOW = NOW.plus_seconds(60)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.db = tmp_path / "i.sqlite3"
        self.world: MarketWorld = market_world()
        self.clock = Clock()

    def open(self, registry: KeyRegistry | None = None, **kwargs: object) -> IntegrationService:
        registry = self.world.registry() if registry is None else registry
        return open_service(
            self.db,
            self.world,
            clock=self.clock,
            registry=registry,
            **kwargs,  # type: ignore[arg-type]
        )

    def settled(self, service: IntegrationService, index: int = 1) -> str:
        report = service.purchase(job(index), agent_signer)
        assert isinstance(report, PurchaseReport) and report.state is S.SETTLED, report
        return report.operation_id

    def rows(self) -> list[tuple[str, str, str]]:
        with sqlite3.connect(self.db) as conn:
            return [
                (str(r[0]), str(r[1]), str(r[2]))
                for r in conn.execute(
                    "SELECT key_id, revoked_by, via FROM integration_key_revocations ORDER BY 1"
                )
            ]

    def offline(self, key: object = A, operator: str = "ops-alice") -> revocation.Revocation:
        return revoke_key(self.db, key, operator, clock=lambda: OFFLINE_NOW)  # type: ignore[arg-type]


def refused(call: Callable[[], object]) -> str:
    with pytest.raises(NotAuthorized) as exc:
        call()
    return exc.value.reason


def fresh_process_check(db: Path) -> dict[str, str]:
    """A new OS process opens the database with a freshly built registry (all keys current)."""
    out = subprocess.run(
        [sys.executable, "-B", str(HERE / "i1_child.py"), str(db), "check_revoked"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=db.parent,
    )
    assert out.returncode == 0, out.stderr
    result: dict[str, str] = json.loads(out.stdout.strip().splitlines()[-1])
    return result


# --- F1-I: Skopje's closed-service sequence ------------------------------------------------------


def test_f1i_revoke_after_close_is_never_a_false_success(tmp_path: Path) -> None:
    env = Env(tmp_path)
    registry = env.world.registry()
    service = env.open(registry, register=(GENERAL_FACILITY,))
    env.settled(service)
    service.close()
    with pytest.raises(RevocationNotPersisted) as exc:
        registry.revoke(A)
    assert "no durable revocation was committed" in str(exc.value)
    assert env.rows() == []  # honest: nothing was persisted
    # this object is marked revoked (fail closed in-process), but that is not durable
    assert registry.lookup(A) is not None
    assert registry.lookup(A).status is KeyStatus.REVOKED  # type: ignore[union-attr]


def test_f1i_offline_revocation_holds_in_a_new_process_with_a_fresh_registry(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    registry = env.world.registry()
    service = env.open(registry, register=(GENERAL_FACILITY,))
    env.settled(service)
    service.close()
    with pytest.raises(RevocationNotPersisted):
        registry.revoke(A)
    record = env.offline()
    assert (record.key_id, record.via, record.revoked_at) == (A, "offline_admin", OFFLINE_NOW)
    assert record.revoked_by.startswith("ops-alice (os uid ")
    result = fresh_process_check(env.db)
    assert result["retrieve"] == "refused REVOKED_KEY"
    assert result["acknowledge"] == "refused REVOKED_KEY"
    assert result["purchase"] == "no_qualifying_offer KEY_REVOKED"
    assert result["provider_calls"] == "0"


def test_f1i_without_a_durable_revocation_a_new_process_is_not_protected(tmp_path: Path) -> None:
    """The closed-service error is truthful: nothing durable happened, so a fresh registry in a
    new process still trusts the key. (The operator was told; see the test above for the fix.)"""
    env = Env(tmp_path)
    registry = env.world.registry()
    service = env.open(registry, register=(GENERAL_FACILITY,))
    env.settled(service)
    service.close()
    with pytest.raises(RevocationNotPersisted):
        registry.revoke(A)
    result = fresh_process_check(env.db)
    assert result["retrieve"] == "allowed"


# --- F1-II: operator lifecycle -------------------------------------------------------------------


def test_f1ii_revoke_while_open_is_durable(tmp_path: Path) -> None:
    env = Env(tmp_path)
    registry = env.world.registry()
    service = env.open(registry, register=(GENERAL_FACILITY,), operator="ops-bob")
    op = env.settled(service)
    registry.revoke(A)  # returns only after commit
    assert env.rows() == [(A.value, "ops-bob", "key_registry_revoke")]
    service.close()
    assert refused(lambda: retrieve(env.open(env.world.registry()), op)) == "REVOKED_KEY"


def test_f1ii_close_twice_keeps_the_safeguard(tmp_path: Path) -> None:
    env = Env(tmp_path)
    registry = env.world.registry()
    service = env.open(registry)
    service.close()
    service.close()
    with pytest.raises(RevocationNotPersisted):
        registry.revoke(A)


def test_f1ii_reopening_the_same_registry_restores_durable_revocation_without_wrappers(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    registry = registry_with_agent_b(env.world)
    first = env.open(registry, register=(GENERAL_FACILITY,))
    op = env.settled(first)
    first.close()
    with pytest.raises(RevocationNotPersisted):
        registry.revoke(A)  # marked in this object only
    second = env.open(registry)  # binding persists what the object already marks revoked
    assert env.rows() == [(A.value, "integration-operator", "key_registry_revoke")]
    bound = vars(registry)["revoke"]
    assert isinstance(bound.__self__, DurableKeyRegistry)
    first.close()  # a stale close of the earlier service must not disturb the new binding
    assert vars(registry)["revoke"] == bound
    registry.revoke(B)  # one durable write through the single current binding
    assert [row[0] for row in env.rows()] == sorted([A.value, B.value])
    second.close()
    assert refused(lambda: retrieve(env.open(env.world.registry()), op)) == "REVOKED_KEY"


def test_f1ii_a_registry_bound_to_an_open_service_cannot_be_bound_again(tmp_path: Path) -> None:
    env = Env(tmp_path)
    registry = env.world.registry()
    other = Env(tmp_path / "other")
    (tmp_path / "other").mkdir(exist_ok=True)
    open_one = env.open(registry)
    with pytest.raises(RegistryAlreadyBound):
        other.open(registry)
    ProcessOwnershipCheck.free(other.db)  # the refused open released its ownership
    open_one.close()
    other.open(registry).close()  # once closed, it can be bound elsewhere


class ProcessOwnershipCheck:
    @staticmethod
    def free(db: Path) -> None:
        from credence.integration.ownership import ProcessOwnership

        ProcessOwnership.acquire(db).release()


def test_f1ii_offline_revocation_before_any_service_open_refuses_to_create_a_database(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "never-opened.sqlite3"
    with pytest.raises(OfflineRevocationRefused) as exc:
        revoke_key(missing, A, "ops-alice")
    assert exc.value.reason == "DATABASE_MISSING"
    assert not missing.exists()


def test_f1ii_offline_revocation_of_an_existing_idle_database(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.open(register=(GENERAL_FACILITY,)).close()  # created earlier; no service running now
    env.offline()
    service = env.open(env.world.registry())  # a fresh registry declaring the key current
    purchase = service.purchase(job(1), agent_signer)
    assert isinstance(purchase, PurchaseReport) and purchase.detail == "KEY_REVOKED"


def test_f1ii_offline_revocation_is_idempotent_and_irreversible(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.open().close()
    first = env.offline(operator="ops-alice")
    again = revoke_key(env.db, A, "ops-carol", clock=lambda: OFFLINE_NOW.plus_seconds(500))
    assert again == first  # the first record is kept
    assert len(env.rows()) == 1
    service = env.open(env.world.registry())
    with pytest.raises(RuntimeError):
        service._registry.register(env.world.registry().lookup(A))  # type: ignore[arg-type]


def test_f1ii_offline_revocation_refuses_while_a_service_runs(tmp_path: Path) -> None:
    env = Env(tmp_path)
    service = env.open()
    with pytest.raises(OwnershipError):
        env.offline()
    assert env.rows() == []
    service.close()


def test_f1ii_offline_revocation_requires_a_trusted_operator_context(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.open().close()
    with pytest.raises(OfflineRevocationRefused) as exc:
        revoke_key(env.db, A, "  ")
    assert exc.value.reason == "OPERATOR_REQUIRED"
    os.chmod(env.db, 0o644)
    with pytest.raises(OfflineRevocationRefused) as exc:
        env.offline()
    assert exc.value.reason == "INSECURE_PERMISSIONS"
    os.chmod(env.db, 0o600)
    stranger = tmp_path / "plain.sqlite3"
    sqlite3.connect(stranger).close()
    os.chmod(stranger, 0o600)
    with pytest.raises(OfflineRevocationRefused) as exc:
        revoke_key(stranger, A, "ops-alice")
    assert exc.value.reason == "NOT_AN_INTEGRATION_DATABASE"
    with sqlite3.connect(stranger) as conn:  # nothing was created in a non-integration file
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone() == (0,)
    with pytest.raises(TypeError):
        revoke_key(env.db, A.value, "ops-alice")  # type: ignore[arg-type]
    assert env.rows() == []


def test_f1ii_offline_persistence_failure_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    env.open().close()

    def broken(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(RevocationStore, "revoke", broken)
    with pytest.raises(RevocationNotPersisted):
        env.offline()
    monkeypatch.undo()
    assert env.rows() == []


def test_f1ii_offline_revocation_upgrades_the_i1g_schema_in_place(tmp_path: Path) -> None:
    env = Env(tmp_path)
    registry = env.world.registry()
    env.open(registry).close()
    with sqlite3.connect(env.db) as conn:  # rebuild the table exactly as I1g created it
        conn.execute("DROP TABLE integration_key_revocations")
        conn.execute(
            "CREATE TABLE integration_key_revocations (key_id TEXT PRIMARY KEY, revoked_at TEXT "
            "NOT NULL, revoked_by TEXT NOT NULL, via TEXT NOT NULL CHECK (via IN "
            "('key_registry_revoke')))"
        )
        conn.execute(
            "INSERT INTO integration_key_revocations VALUES (?, ?, 'ops', 'key_registry_revoke')",
            (B.value, NOW.value),
        )
    env.offline()
    assert {(key, via) for key, _, via in env.rows()} == {
        (B.value, "key_registry_revoke"),  # the I1g record survived the upgrade
        (A.value, "offline_admin"),
    }


def test_f1ii_command_line_offline_revocation(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.open().close()
    command = [sys.executable, "-B", "-m", "credence.integration.revocation"]
    ok = subprocess.run(
        [*command, "--database", str(env.db), "--key-id", A.value, "--operator", "ops-alice"],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
    )
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.startswith(f"revoked {A.value} at ")
    missing = subprocess.run(
        [*command, "--database", str(tmp_path / "nope"), "--key-id", A.value, "--operator", "x"],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
    )
    assert missing.returncode == 2 and "DATABASE_MISSING" in missing.stderr
    assert not (tmp_path / "nope").exists()


def test_f1ii_unrelated_agents_are_unaffected(tmp_path: Path) -> None:
    env = Env(tmp_path)
    service = env.open(registry_with_agent_b(env.world), register=(GENERAL_FACILITY,))
    op = env.settled(service)
    service.close()
    env.offline(B)
    reopened = env.open(registry_with_agent_b(env.world))
    assert retrieve(reopened, op)
    env.settled(reopened, 2)


def _purchase_vs_revoke(path: Path) -> str:
    env = Env(path)
    registry = env.world.registry()
    service = env.open(registry, register=(GENERAL_FACILITY,))
    barrier = threading.Barrier(2)
    out: dict[str, object] = {}

    def buy() -> None:
        barrier.wait()
        out["report"] = service.purchase(job(1), agent_signer)

    def revoke() -> None:
        barrier.wait()
        registry.revoke(A)

    threads = [threading.Thread(target=buy), threading.Thread(target=revoke)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    report = out["report"]
    assert isinstance(report, PurchaseReport)
    # before revocation: settled; revoked before qualification, reservation or execution:
    # no qualifying offer, policy rejection (KEY_REVOKED at reserve) or audited release
    assert report.state in {
        S.SETTLED,
        S.NO_QUALIFYING_OFFER,
        S.POLICY_REJECTED,
        S.RELEASED_UNDELIVERED,
    }, report
    if report.state is S.POLICY_REJECTED:
        assert report.reason_code == "KEY_REVOKED"
    after = service.purchase(job(2), agent_signer)  # once committed, always refused
    assert isinstance(after, PurchaseReport) and after.detail == "KEY_REVOKED"
    service.close()
    return str(report.state.value)


def test_f1ii_revocation_racing_purchases_is_consistent(tmp_path: Path) -> None:
    outcomes = []
    for round_ in range(20):
        (tmp_path / str(round_)).mkdir()
        outcomes.append(_purchase_vs_revoke(tmp_path / str(round_)))
    assert set(outcomes) <= {
        "settled",
        "no_qualifying_offer",
        "policy_rejected",
        "released_undelivered",
    }


# --- F1-III: no persistence (no I/O) inside lookup ----------------------------------------------


@pytest.fixture
def no_io_in_lookup(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Any sqlite3.connect made while DurableKeyRegistry.lookup runs fails the test."""
    inside = threading.local()
    counts = {"lookups": 0, "connects_inside": 0}
    real_connect = sqlite3.connect
    real_lookup = DurableKeyRegistry.lookup

    def guarded_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        if getattr(inside, "active", False):
            counts["connects_inside"] += 1
            raise AssertionError("database I/O inside DurableKeyRegistry.lookup")
        connection: sqlite3.Connection = real_connect(*args, **kwargs)
        return connection

    def counted_lookup(self: DurableKeyRegistry, key_id: object) -> object:
        counts["lookups"] += 1
        inside.active = True
        try:
            return real_lookup(self, key_id)  # type: ignore[arg-type]
        finally:
            inside.active = False

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    monkeypatch.setattr(DurableKeyRegistry, "lookup", counted_lookup)
    return counts


def test_f1iii_class_method_revocation_of_the_gateway_key_fails_closed_promptly(
    tmp_path: Path, no_io_in_lookup: dict[str, int]
) -> None:
    env = Env(tmp_path)
    registry = env.world.registry()
    service = env.open(registry, register=(GENERAL_FACILITY,))
    KeyRegistry.revoke(registry, G)  # bypasses the bound instance method
    started = time.monotonic()
    report = service.purchase(job(1), agent_signer)
    elapsed = time.monotonic() - started
    assert elapsed < 5  # no 30 s busy-timeout wait (it was 31.1 s at 1f82874)
    assert isinstance(report, PurchaseReport)
    assert (report.state, report.reason_code) == (S.DISPUTED, "RECEIPT_SIG_INVALID")
    with sqlite3.connect(env.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounting_settlements").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM no_delivery_releases").fetchone() == (0,)
    assert len(env.world.nebius_transport.posts) == 1  # the one delivery; never re-executed
    assert env.rows() == []  # nothing persisted from inside lookup
    assert no_io_in_lookup["lookups"] > 0 and no_io_in_lookup["connects_inside"] == 0


def test_f1iii_class_method_revocation_of_the_agent_key_fails_closed_in_process(
    tmp_path: Path, no_io_in_lookup: dict[str, int]
) -> None:
    env = Env(tmp_path)
    registry = env.world.registry()
    service = env.open(registry, register=(GENERAL_FACILITY,))
    op = env.settled(service)
    KeyRegistry.revoke(registry, A)
    assert refused(lambda: retrieve(service, op)) == "REVOKED_KEY"
    assert refused(lambda: acknowledge(service, op)) == "REVOKED_KEY"
    purchase = service.purchase(job(2), agent_signer)
    assert isinstance(purchase, PurchaseReport) and purchase.detail == "KEY_REVOKED"
    assert env.rows() == []  # in-process refusal only; durable needs bound or offline revocation
    assert no_io_in_lookup["connects_inside"] == 0


def test_f1iii_lookup_never_touches_the_store_on_any_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    registry = registry_with_agent_b(env.world)
    service = env.open(registry)
    registry.revoke(B)  # persisted (mirror)
    KeyRegistry.revoke(registry, G)  # unexpected in-memory revocation
    durable = vars(registry)["revoke"].__self__
    durable._refused_in_process.add(A)  # a failed-persistence refusal

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("store touched from lookup")

    for name in ("get", "revoke", "all", "_write"):
        monkeypatch.setattr(RevocationStore, name, forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    unknown = key_id_for(type(AGENT_KEY)(b"\x05" * 32).verify_key)
    assert durable.lookup(unknown) is None
    for key in (A, B, G):
        found = durable.lookup(key)
        assert found is not None and found.status is KeyStatus.REVOKED
    monkeypatch.undo()
    service.close()
