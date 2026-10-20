"""Integration I1c: one gateway process per integration database.

Ownership is an OS-level flock on `<database>.owner`. These tests use a real second process,
including one killed with SIGKILL, so the crash behaviour is the operating system's, not a flag.
Offline: no network, model or XRPL.
"""

from __future__ import annotations

import os
import signal
import socket
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from i1_world import GENERAL_FACILITY, agent_signer, job, market_world, open_service

from credence.integration.journal import OperationState as S
from credence.integration.ownership import OwnershipError, ProcessOwnership, owner_lock_path
from credence.integration.service import PurchaseReport

REPO = Path(__file__).resolve().parents[2]

HOLDER = """
import sys, time
from pathlib import Path
from credence.integration.ownership import ProcessOwnership
ownership = ProcessOwnership.acquire(Path(sys.argv[1]))
print("owned", flush=True)
time.sleep(120)
"""


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


@pytest.fixture
def holder(tmp_path: Path) -> Iterator[tuple[subprocess.Popen[str], Path]]:
    """A separate OS process that owns the database until it is killed."""
    db = tmp_path / "i.sqlite3"
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(db)],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "owned"
    try:
        yield proc, db
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def test_a_second_process_cannot_serve_the_same_database(
    holder: tuple[subprocess.Popen[str], Path],
) -> None:
    _, db = holder
    with pytest.raises(OwnershipError):
        open_service(db, market_world())


def test_ownership_returns_when_the_owner_is_killed(
    holder: tuple[subprocess.Popen[str], Path],
) -> None:
    proc, db = holder
    with pytest.raises(OwnershipError):
        ProcessOwnership.acquire(db)
    proc.send_signal(signal.SIGKILL)  # no cleanup runs in the killed process
    proc.wait(timeout=10)
    service = open_service(db, market_world(), register=(GENERAL_FACILITY,))
    report = service.purchase(job(1), agent_signer)
    assert isinstance(report, PurchaseReport) and report.state is S.SETTLED


def test_a_second_service_in_the_same_process_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "i.sqlite3"
    first = open_service(db, market_world())
    with pytest.raises(OwnershipError):
        open_service(db, market_world())
    first.close()
    second = open_service(db, market_world())
    second.close()


def test_a_closed_service_refuses_work(tmp_path: Path) -> None:
    service = open_service(tmp_path / "i.sqlite3", market_world(), register=(GENERAL_FACILITY,))
    service.close()
    with pytest.raises(RuntimeError):
        service.purchase(job(1), agent_signer)


def test_database_and_owner_files_are_owner_only(tmp_path: Path) -> None:
    db = tmp_path / "i.sqlite3"
    service = open_service(db, market_world(), register=(GENERAL_FACILITY,))
    service.purchase(job(1), agent_signer)
    for path in (db, owner_lock_path(db)):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    service.close()
