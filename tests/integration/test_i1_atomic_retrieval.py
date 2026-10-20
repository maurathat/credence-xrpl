"""Integration I1g, finding F2: retrieval is atomic with nonce consumption.

The Inference owner's reproduction: retrieval authenticated and committed its nonce and state, then
an acknowledgment deleted the response before retrieval read it, and retrieval raised
AssertionError with its nonce consumed. Now the availability check, nonce consumption, retrieval
record and the read of the exact bytes are one SQLite transaction. Each journal operation uses its
own connection. Offline: fake providers; no network, model or XRPL.
"""

from __future__ import annotations

import socket
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from i1_world import (
    GENERAL_FACILITY,
    acknowledge,
    agent_signer,
    authorize,
    job,
    market_world,
    open_service,
    retrieve,
)

from credence.integration.authorization import ResponseAction
from credence.integration.journal import OperationState as S
from credence.integration.service import IntegrationService, PurchaseReport, ResponseUnavailable

R, ACK = ResponseAction.RETRIEVE, ResponseAction.ACKNOWLEDGE


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def settled(tmp_path: Path) -> tuple[IntegrationService, str, bytes, Path]:
    db = tmp_path / "i.sqlite3"
    service = open_service(db, market_world(), register=(GENERAL_FACILITY,))
    report = service.purchase(job(1), agent_signer)
    assert isinstance(report, PurchaseReport) and report.state is S.SETTLED
    response = service.journal_entry(report.operation_id).response
    assert response
    return service, report.operation_id, response, db


def nonce_consumed(db: Path, nonce: str) -> bool:
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM integration_authorization_nonces WHERE nonce = ?", (nonce,)
        ).fetchone()
    return bool(row[0])


def test_f2_owner_interleaving_returns_a_typed_refusal_and_keeps_the_nonce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An acknowledgment commits after retrieval authenticated but before its transaction."""
    service, op, response, db = settled(tmp_path)
    assert retrieve(service, op) == response  # the agent has the bytes; now it may acknowledge
    original = IntegrationService._authorize
    interleaved = {"done": False}

    def authorize_then_acknowledge(
        self: IntegrationService, authorization: object, action: ResponseAction
    ) -> object:
        op_row = original(self, authorization, action)  # type: ignore[arg-type]
        if action is R and not interleaved["done"]:
            interleaved["done"] = True
            acknowledge(self, op)  # another caller, another connection
        return op_row

    monkeypatch.setattr(IntegrationService, "_authorize", authorize_then_acknowledge)
    late = authorize(service, op, R)
    with pytest.raises(ResponseUnavailable) as exc:  # not AssertionError
        service.retrieve(late)
    assert exc.value.state is S.ACKNOWLEDGED
    assert not nonce_consumed(db, late.nonce)  # the refused retrieval consumed nothing


def test_f2_acknowledge_then_retrieve(tmp_path: Path) -> None:
    service, op, response, db = settled(tmp_path)
    assert retrieve(service, op) == response
    acknowledge(service, op)
    late = authorize(service, op, R)
    with pytest.raises(ResponseUnavailable) as exc:
        service.retrieve(late)
    assert exc.value.state is S.ACKNOWLEDGED and not nonce_consumed(db, late.nonce)


def test_f2_retrieve_then_acknowledge(tmp_path: Path) -> None:
    service, op, response, db = settled(tmp_path)
    first = authorize(service, op, R)
    assert service.retrieve(first) == response and nonce_consumed(db, first.nonce)
    again = authorize(service, op, R)
    assert service.retrieve(again) == response  # a retained response can be fetched again
    ack = authorize(service, op, ACK)
    service.acknowledge(ack)
    assert nonce_consumed(db, ack.nonce)
    assert service.journal_entry(op).response is None


def test_f2_acknowledge_before_any_retrieval_is_refused_without_consuming(tmp_path: Path) -> None:
    service, op, _, db = settled(tmp_path)
    ack = authorize(service, op, ACK)
    with pytest.raises(ResponseUnavailable) as exc:
        service.acknowledge(ack)
    assert exc.value.state is S.SETTLED and not nonce_consumed(db, ack.nonce)
    assert service.journal_entry(op).response is not None


def _race(tmp_path: Path) -> str:
    service, op, response, db = settled(tmp_path)
    assert retrieve(service, op) == response  # state: retrieved
    get = authorize(service, op, R)
    ack = authorize(service, op, ACK)
    barrier = threading.Barrier(2)
    out: dict[str, object] = {}

    def do_retrieve() -> None:
        barrier.wait()
        try:
            out["retrieve"] = service.retrieve(get)
        except ResponseUnavailable as exc:
            out["retrieve"] = exc.state
        except BaseException as exc:  # recorded: must never happen
            out["retrieve"] = exc

    def do_acknowledge() -> None:
        barrier.wait()
        service.acknowledge(ack)
        out["ack"] = "ok"

    threads = [threading.Thread(target=do_retrieve), threading.Thread(target=do_acknowledge)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert out.get("ack") == "ok"
    service.close()
    if isinstance(out["retrieve"], bytes):
        assert out["retrieve"] == response  # exact bytes
        assert nonce_consumed(db, get.nonce)
        return "retrieve_first"
    assert out["retrieve"] is S.ACKNOWLEDGED, out["retrieve"]
    assert not nonce_consumed(db, get.nonce)
    return "acknowledge_first"


def test_f2_concurrent_retrieve_and_acknowledge_serialize(tmp_path: Path) -> None:
    outcomes = []
    for round_ in range(30):
        (tmp_path / str(round_)).mkdir()
        outcomes.append(_race(tmp_path / str(round_)))
    assert set(outcomes) <= {"retrieve_first", "acknowledge_first"}
