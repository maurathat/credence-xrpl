"""Integration I1e: recovery after a real process is killed (SIGKILL) mid-purchase.

A child process (i1_child.py) runs the integrated service and stops at a lifecycle point; the
parent kills it, so no cleanup or exception handler runs. The parent then opens the same database
(the OS has released the child's ownership lock) and recovers. Recovery must never repeat an
uncertain chargeable execution, settle twice, release a payable delivery, advance the receipt chain
without accounting settlement, or lose a legitimately recoverable response. Offline: fake
providers in both processes; no network, model or XRPL.
"""

from __future__ import annotations

import hashlib
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from i1_world import (
    GENERAL_FACILITY,
    Clock,
    acknowledge,
    count,
    market_world,
    open_service,
    retrieve,
)
from inference_world import NOW
from market_world import MarketWorld

from credence.core.primitives import ReservationId, ReservationStatus, Sha256Digest
from credence.core.settlement_coordinator import UnknownDeliveryCause
from credence.integration.journal import OperationState as S
from credence.integration.ownership import OwnershipError
from credence.integration.service import IntegrationService, operation_id_for

HERE = Path(__file__).resolve().parent
CHILD = HERE / "i1_child.py"
OP = operation_id_for("job-facility-market-general-1")
FID = GENERAL_FACILITY.facility_id


Child = tuple[Path, str, "subprocess.Popen[str]"]


@pytest.fixture
def run_child(tmp_path: Path) -> Iterator[Callable[[str], Child]]:
    """Start i1_child.py for a scenario; return once it reports its stopping point."""
    procs: list[subprocess.Popen[str]] = []

    def start(scenario: str) -> Child:
        db = tmp_path / "i.sqlite3"
        proc = subprocess.Popen(
            [sys.executable, "-B", str(CHILD), str(db), scenario],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=tmp_path,
        )
        procs.append(proc)
        assert proc.stdout is not None
        marker = proc.stdout.readline().strip()
        if not marker:
            proc.wait(timeout=10)
            raise AssertionError(proc.stderr.read() if proc.stderr else "child gave no marker")
        return db, marker, proc

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def killed_and_recovered(
    run_child: Callable[[str], Child], scenario: str
) -> tuple[IntegrationService, Path, str, MarketWorld]:
    db, marker, proc = run_child(scenario)
    # While the child is alive it owns the database: a second server is refused.
    with pytest.raises(OwnershipError):
        open_service(db, market_world())
    # No SQLite write transaction is open at the child's stopping point.
    conn = sqlite3.connect(db, timeout=0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    proc.send_signal(signal.SIGKILL)  # no cleanup runs in the child
    proc.wait(timeout=10)
    world = market_world()  # the parent's providers: any call here would be a re-execution
    service = open_service(db, world, clock=Clock(NOW.plus_seconds(120)))  # recovers on open
    return service, db, marker, world


def assert_not_charged_or_chained(service: IntegrationService, db: Path) -> None:
    assert count(db, "SELECT COUNT(*) FROM accounting_settlements") == 0
    assert service.accounting.exposure(FID, NOW.plus_seconds(130)).drawn.value == 0
    assert service.accounting.chain_head(FID) is None


def test_killed_during_the_provider_call_is_unknown_never_reexecuted(
    run_child: Callable[[str], Child],
) -> None:
    service, db, marker, world = killed_and_recovered(run_child, "provider")
    assert marker == "IN_PROVIDER"
    op = service.journal_entry(OP)
    assert op.state is S.UNKNOWN and op.response is None and op.receipt_digest is None
    rid = ReservationId(str(op.reservation_id))
    attempt = service.accounting.delivery_attempt(rid)
    assert attempt is not None
    assert attempt.execution_claimed_at is not None  # Core authorized exactly this one call
    assert attempt.unknown_cause is UnknownDeliveryCause.INTERRUPTED
    assert service.accounting.reservation(rid).status is ReservationStatus.HELD  # not released
    assert service.accounting.no_delivery_releases() == []
    assert_not_charged_or_chained(service, db)
    service.recover()
    assert world.nebius_transport.posts == [] and world.local.calls == 0  # never re-executed


def test_killed_after_begin_delivery_before_evidence_is_unknown(
    run_child: Callable[[str], Child],
) -> None:
    service, db, marker, world = killed_and_recovered(run_child, "attempt")
    assert marker == "AT_AFTER_ATTEMPT_BEGUN"
    op = service.journal_entry(OP)
    assert op.state is S.UNKNOWN
    rid = ReservationId(str(op.reservation_id))
    attempt = service.accounting.delivery_attempt(rid)
    assert attempt is not None and attempt.execution_claimed_at is None
    assert service.accounting.reservation(rid).status is ReservationStatus.HELD
    assert service.accounting.no_delivery_releases() == []  # never falsely released
    assert_not_charged_or_chained(service, db)
    assert world.nebius_transport.posts == []


def test_killed_after_evidence_before_settlement_settles_once(
    run_child: Callable[[str], Child],
) -> None:
    service, db, marker, world = killed_and_recovered(run_child, "evidence")
    assert marker == "AT_AFTER_EVIDENCE_STORED"
    op = service.journal_entry(OP)
    assert op.state is S.SETTLED
    assert count(db, "SELECT COUNT(*) FROM accounting_settlements") == 1
    assert service.accounting.chain_head(FID) == Sha256Digest(str(op.receipt_digest))
    service.recover()
    assert count(db, "SELECT COUNT(*) FROM accounting_settlements") == 1  # never twice
    assert retrieve(service, OP) == op.response
    assert world.nebius_transport.posts == []


def test_killed_after_retrieval_before_acknowledgment_keeps_the_response(
    run_child: Callable[[str], Child],
) -> None:
    service, db, marker, world = killed_and_recovered(run_child, "retrieved")
    digest = marker.removeprefix("RETRIEVED ")
    op = service.journal_entry(OP)
    assert op.state is S.RETRIEVED and op.response is not None
    response = retrieve(service, OP)
    assert hashlib.sha256(response).hexdigest() == digest  # the same bytes the child received
    acknowledge(service, OP)
    assert service.journal_entry(OP).response is None
    assert count(db, "SELECT COUNT(*) FROM accounting_settlements") == 1
    assert world.nebius_transport.posts == []
