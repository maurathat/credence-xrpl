"""Integration I1g, finding F1: durable, fail-closed agent-key revocation across restarts.

The Inference owner's reproduction: revoke in memory, restart with a freshly built registry that
marks the key current, and the key regained retrieval, acknowledgment and purchase. Here the
existing workflow (`KeyRegistry.revoke` on the registry the service was opened with) persists the
revocation, and persisted revocations override any registry supplied later. Real signatures and
registry; offline (fake providers); no network, model or XRPL.
"""

from __future__ import annotations

import socket
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from i1_world import (
    AGENT_B_KEY,
    GENERAL_FACILITY,
    Clock,
    acknowledge,
    agent_signer,
    authorize,
    job,
    market_world,
    open_service,
    registry_with_agent_b,
    retrieve,
)
from inference_world import AGENT_KEY
from market_world import MarketWorld

from credence.core.signatures import KeyRegistry, KeyRegistryError, KeyStatus, key_id_for
from credence.integration.authorization import NotAuthorized, ResponseAction
from credence.integration.journal import OperationState as S
from credence.integration.ownership import ProcessOwnership
from credence.integration.revocation import (
    RevocationNotPersisted,
    RevocationStateError,
    RevocationStore,
)
from credence.integration.service import IntegrationService, PurchaseReport

A = key_id_for(AGENT_KEY.verify_key)
R, ACK = ResponseAction.RETRIEVE, ResponseAction.ACKNOWLEDGE


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

    def settled(self, service: IntegrationService, index: int) -> str:
        report = service.purchase(job(index), agent_signer)
        assert isinstance(report, PurchaseReport) and report.state is S.SETTLED, report
        return report.operation_id


def refused(call: object) -> str:
    with pytest.raises(NotAuthorized) as exc:
        call()  # type: ignore[operator]
    return exc.value.reason


def test_f1_owner_reproduction_revocation_survives_restart_with_a_fresh_registry(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    registry = env.world.registry()
    service = env.open(registry, register=(GENERAL_FACILITY,))
    op = env.settled(service, 1)
    registry.revoke(A)  # the existing in-memory workflow
    assert refused(lambda: retrieve(service, op)) == "REVOKED_KEY"
    service.close()

    fresh = env.world.registry()  # a freshly built registry marks the key current again
    assert fresh.lookup(A) is not None and fresh.lookup(A).status is KeyStatus.CURRENT  # type: ignore[union-attr]
    reopened = env.open(fresh)
    assert refused(lambda: retrieve(reopened, op)) == "REVOKED_KEY"
    assert refused(lambda: acknowledge(reopened, op)) == "REVOKED_KEY"
    assert reopened.journal_entry(op).response is not None  # not deleted
    purchase = reopened.purchase(job(2), agent_signer)
    assert isinstance(purchase, PurchaseReport)  # Core qualifies no offer for a revoked key
    assert (purchase.state, purchase.detail) == (S.NO_QUALIFYING_OFFER, "KEY_REVOKED")
    assert len(env.world.nebius_transport.posts) == 1  # only the first, pre-revocation purchase


def test_f1_revocation_record_names_key_time_and_actor_and_never_expires(tmp_path: Path) -> None:
    env = Env(tmp_path)
    registry = env.world.registry()
    service = env.open(registry, register=(GENERAL_FACILITY,), operator="ops-alice")
    op = env.settled(service, 1)
    registry.revoke(A)
    record = RevocationStore(env.db).get(A)
    assert record is not None
    assert (record.key_id, record.revoked_by, record.via) == (A, "ops-alice", "key_registry_revoke")
    service.close()
    env.clock.advance(10 * 365 * 24 * 3600)  # ten years later
    for _ in range(3):  # repeated restarts, each with a fresh registry
        _assert_revoked_after_reopen(env, op)


def _assert_revoked_after_reopen(env: Env, op: str) -> None:
    reopened = env.open()
    assert refused(lambda: retrieve(reopened, op)) == "REVOKED_KEY"
    reopened.close()


def test_f1_re_registering_the_revoked_key_does_not_restore_it(tmp_path: Path) -> None:
    env = Env(tmp_path)
    registry = env.world.registry()
    service = env.open(registry, register=(GENERAL_FACILITY,))
    op = env.settled(service, 1)
    registry.revoke(A)
    current = env.world.registry().lookup(A)
    assert current is not None
    with pytest.raises(KeyRegistryError):  # the registry refuses a duplicate key, as before
        registry.register(current)
    service.close()
    reopened = env.open(env.world.registry())  # A registered again, as current
    assert refused(lambda: retrieve(reopened, op)) == "REVOKED_KEY"


def test_f1_revocation_made_before_the_service_opened_is_persisted(tmp_path: Path) -> None:
    env = Env(tmp_path)
    service = env.open(register=(GENERAL_FACILITY,))
    op = env.settled(service, 1)
    service.close()
    registry = env.world.registry()
    registry.revoke(A)  # plain in-memory revocation while no service is open
    env.open(registry).close()  # opening persists it
    reopened = env.open(env.world.registry())
    assert refused(lambda: retrieve(reopened, op)) == "REVOKED_KEY"


def test_f1_unrelated_keys_are_unaffected(tmp_path: Path) -> None:
    env = Env(tmp_path)
    registry = registry_with_agent_b(env.world)
    service = env.open(registry, register=(GENERAL_FACILITY,))
    registry.revoke(key_id_for(AGENT_B_KEY.verify_key))
    op = env.settled(service, 1)
    assert retrieve(service, op)
    service.close()
    reopened = env.open(registry_with_agent_b(env.world))
    assert retrieve(reopened, op) and env.settled(reopened, 2)


def test_f1_closing_the_service_never_reverts_to_silent_in_memory_revoke(tmp_path: Path) -> None:
    """I1i: after close, revoke raises RevocationNotPersisted instead of silently reverting to
    Core's in-memory method (see test_i1_revocation_lifecycle.py for the full lifecycle)."""
    env = Env(tmp_path)
    registry = env.world.registry()
    service = env.open(registry)
    assert "revoke" in vars(registry)
    service.close()
    assert "revoke" in vars(registry)  # still overridden: not Core's silent method
    with pytest.raises(RevocationNotPersisted):
        registry.revoke(A)


def test_f1_persistence_failure_still_refuses_in_this_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    registry = env.world.registry()
    service = env.open(registry, register=(GENERAL_FACILITY,))
    op = env.settled(service, 1)

    def broken(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(RevocationStore, "revoke", broken)
    with pytest.raises(RevocationNotPersisted):
        registry.revoke(A)
    assert refused(lambda: retrieve(service, op)) == "REVOKED_KEY"  # fail closed in-process
    purchase = service.purchase(job(2), agent_signer)
    assert isinstance(purchase, PurchaseReport) and purchase.detail == "KEY_REVOKED"


def test_f1_malformed_revocation_state_refuses_to_open(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.open(register=(GENERAL_FACILITY,)).close()
    with sqlite3.connect(env.db) as conn:
        conn.execute(
            "INSERT INTO integration_key_revocations VALUES ('not-a-key', 'yesterday', '', "
            "'key_registry_revoke')"
        )
    with pytest.raises(RevocationStateError):
        env.open()
    with pytest.raises(RevocationStateError):  # still refused: it never silently recovers
        env.open()
    ProcessOwnership.acquire(env.db).release()  # the failed opens did not leak ownership


def test_f1_unreadable_revocation_state_refuses_access(tmp_path: Path) -> None:
    env = Env(tmp_path)
    service = env.open(register=(GENERAL_FACILITY,))
    op = env.settled(service, 1)
    with sqlite3.connect(env.db) as conn:
        conn.execute("DROP TABLE integration_key_revocations")
    with pytest.raises(RevocationStateError):
        retrieve(service, op)
    with pytest.raises(RevocationStateError):
        service.purchase(job(2), agent_signer)
    assert service.journal_entry(op).state is S.SETTLED


# --- authorization races -------------------------------------------------------------------------


def test_f1_revocation_committed_after_verification_still_refuses_retrieval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signature verification passes, then the revocation commits before the access transaction:
    the in-transaction re-check refuses, and the nonce is not consumed."""
    from credence.integration.authorization import verify_response_authorization as original

    env = Env(tmp_path)
    registry = env.world.registry()
    service = env.open(registry, register=(GENERAL_FACILITY,))
    op = env.settled(service, 1)

    def verify_then_revoke(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)  # type: ignore[arg-type]
        registry.revoke(A)

    monkeypatch.setattr(
        "credence.integration.service.verify_response_authorization", verify_then_revoke
    )
    auth = authorize(service, op, R)
    assert refused(lambda: service.retrieve(auth)) == "REVOKED_KEY"
    with sqlite3.connect(env.db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM integration_authorization_nonces WHERE nonce = ?", (auth.nonce,)
        ).fetchone() == (0,)


def test_f1_purchase_admitted_before_revocation_does_not_execute(tmp_path: Path) -> None:
    """Reserved, then revoked before execution: no provider call; Core's audited release."""
    env = Env(tmp_path)
    registry = env.world.registry()

    def revoke_after_reserve(point: str) -> None:
        if point == "after_reserve":
            registry.revoke(A)

    service = env.open(registry, register=(GENERAL_FACILITY,), crash_hook=revoke_after_reserve)
    report = service.purchase(job(1), agent_signer)
    assert isinstance(report, PurchaseReport) and report.state is S.RELEASED_UNDELIVERED
    assert report.detail == "agent key revoked before execution"
    assert env.world.nebius_transport.posts == []
    (release,) = service.accounting.no_delivery_releases()
    assert release.reservation_id.value == report.reservation_id


def test_f1_execution_begun_before_revocation_settles_but_response_is_refused(
    tmp_path: Path,
) -> None:
    """Revoked after Core authorized execution: the delivery is payable and settles; the revoked
    key cannot retrieve it."""
    env = Env(tmp_path)
    registry = env.world.registry()

    def revoke_after_attempt(point: str) -> None:
        if point == "after_attempt_begun":
            registry.revoke(A)

    service = env.open(registry, register=(GENERAL_FACILITY,), crash_hook=revoke_after_attempt)
    report = service.purchase(job(1), agent_signer)
    assert isinstance(report, PurchaseReport) and report.state is S.SETTLED
    assert refused(lambda: retrieve(service, report.operation_id)) == "REVOKED_KEY"


def _race_once(path: Path) -> str:
    env = Env(path)
    registry = env.world.registry()
    service = env.open(registry, register=(GENERAL_FACILITY,))
    op = env.settled(service, 1)
    auth = authorize(service, op, R)
    barrier = threading.Barrier(2)
    out: dict[str, object] = {}

    def do_retrieve() -> None:
        barrier.wait()
        try:
            out["retrieve"] = service.retrieve(auth)
        except NotAuthorized as exc:
            out["retrieve"] = exc.reason

    def do_revoke() -> None:
        barrier.wait()
        registry.revoke(A)

    threads = [threading.Thread(target=do_retrieve), threading.Thread(target=do_revoke)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    with sqlite3.connect(env.db) as conn:
        consumed = conn.execute(
            "SELECT COUNT(*) FROM integration_authorization_nonces WHERE nonce = ?",
            (auth.nonce,),
        ).fetchone()[0]
    assert refused(lambda: retrieve(service, op)) == "REVOKED_KEY"  # afterwards: always
    service.close()
    if isinstance(out["retrieve"], bytes):
        assert consumed == 1  # retrieved before the revocation committed
        return "retrieve_first"
    assert (out["retrieve"], consumed) == ("REVOKED_KEY", 0)
    return "revoke_first"


def test_f1_concurrent_revocation_and_retrieval_are_consistent(tmp_path: Path) -> None:
    outcomes = []
    for round_ in range(25):
        (tmp_path / str(round_)).mkdir()
        outcomes.append(_race_once(tmp_path / str(round_)))
    assert set(outcomes) <= {"retrieve_first", "revoke_first"}
