"""Integration I1e: agent-signed authorization for response retrieval and acknowledgment.

Inference finding I-3 / Core finding C-3 (reproduction R1): before I1e, anyone knowing an
operation ID and the agent's public key ID could read the response and delete it. Now each access
needs an Ed25519 signature by the registered, current agent key that signed the purchase, over a
domain-separated statement naming the operation, the action and a single-use nonce. Real
signatures and the real registry; nothing is mocked. Offline: no network, model or XRPL.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from dataclasses import replace
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
from inference_world import AGENT_KEY, GATEWAY_KEY

from credence.core.primitives import Ed25519Signature
from credence.core.signatures import KeyRegistry, key_id_for
from credence.integration.authorization import (
    AUTHORIZATION_DOMAIN,
    MAX_LIFETIME_SECONDS,
    NotAuthorized,
    ResponseAction,
    sign_response_authorization,
    statement_bytes,
)
from credence.integration.journal import OperationState as S
from credence.integration.service import (
    IntegrationService,
    PurchaseReport,
    ResponseUnavailable,
)

R, A = ResponseAction.RETRIEVE, ResponseAction.ACKNOWLEDGE


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def settled(
    tmp_path: Path, registry: KeyRegistry | None = None, *, jobs: int = 1
) -> tuple[IntegrationService, list[str], KeyRegistry]:
    world = market_world()
    registry = registry_with_agent_b(world) if registry is None else registry
    service = open_service(
        tmp_path / "i.sqlite3", world, register=(GENERAL_FACILITY,), registry=registry
    )
    ids = []
    for index in range(1, jobs + 1):
        report = service.purchase(job(index), agent_signer)
        assert isinstance(report, PurchaseReport) and report.state is S.SETTLED
        ids.append(report.operation_id)
    return service, ids, registry


def refused(call: object) -> str:
    with pytest.raises(NotAuthorized) as exc:
        call()  # type: ignore[operator]
    return exc.value.reason


def test_r1_public_key_id_alone_reads_and_deletes_nothing(tmp_path: Path) -> None:
    """The exact R1 sequence: B knows A's operation ID and public key ID. B can neither read A's
    response nor delete it; A still retrieves and acknowledges with A's own signatures."""
    service, (op,), _ = settled(tmp_path)
    a_key_id = key_id_for(AGENT_KEY.verify_key)
    impersonation = replace(
        authorize(service, op, R, key=AGENT_B_KEY), agent_key_id=a_key_id
    )  # A's public ID, B's signature
    assert refused(lambda: service.retrieve(impersonation)) == "BAD_SIGNATURE"
    forged_ack = replace(authorize(service, op, A, key=AGENT_B_KEY), agent_key_id=a_key_id)
    assert refused(lambda: service.acknowledge(forged_ack)) == "BAD_SIGNATURE"
    entry = service.journal_entry(op)
    assert entry.state is S.SETTLED and entry.response is not None  # nothing read or deleted
    assert retrieve(service, op) == entry.response
    acknowledge(service, op)
    assert service.journal_entry(op).response is None


def test_cross_agent_retrieval_and_acknowledgment_are_refused(tmp_path: Path) -> None:
    service, (op,), _ = settled(tmp_path)
    assert refused(lambda: service.retrieve(authorize(service, op, R, key=AGENT_B_KEY))) == (
        "WRONG_AGENT"
    )
    retrieve(service, op)
    assert refused(lambda: service.acknowledge(authorize(service, op, A, key=AGENT_B_KEY))) == (
        "WRONG_AGENT"
    )
    assert service.journal_entry(op).state is S.RETRIEVED


def test_non_agent_and_unregistered_keys_are_refused(tmp_path: Path) -> None:
    from nacl.signing import SigningKey

    service, (op,), _ = settled(tmp_path)
    gateway_signed = authorize(service, op, R, key=GATEWAY_KEY)  # registered, role gateway
    assert refused(lambda: service.retrieve(gateway_signed)) == "WRONG_ROLE"
    stranger = authorize(service, op, R, key=SigningKey(b"\x07" * 32))
    assert refused(lambda: service.retrieve(stranger)) == "UNKNOWN_KEY"


def test_revoked_key_cannot_retrieve(tmp_path: Path) -> None:
    service, (op,), registry = settled(tmp_path)
    registry.revoke(key_id_for(AGENT_KEY.verify_key))
    assert refused(lambda: service.retrieve(authorize(service, op, R))) == "REVOKED_KEY"
    assert service.journal_entry(op).state is S.SETTLED


def test_replayed_retrieval_authorization_is_refused(tmp_path: Path) -> None:
    service, (op,), _ = settled(tmp_path)
    once = authorize(service, op, R)
    first = service.retrieve(once)
    assert refused(lambda: service.retrieve(once)) == "NONCE_REUSED"
    assert service.retrieve(authorize(service, op, R)) == first  # a fresh one works


def test_replayed_acknowledgment_authorization_is_refused(tmp_path: Path) -> None:
    service, (op, other), _ = settled(tmp_path, jobs=2)
    retrieve(service, op)
    ack = authorize(service, op, A)
    service.acknowledge(ack)
    with pytest.raises(ResponseUnavailable) as exc:  # already acknowledged: nothing to delete
        service.acknowledge(ack)
    assert exc.value.state is S.ACKNOWLEDGED
    # a nonce is single-use per agent key across actions and operations
    nonce = "cd" * 16
    service.retrieve(authorize(service, other, R, nonce=nonce))
    reused = authorize(service, other, A, nonce=nonce)
    assert refused(lambda: service.acknowledge(reused)) == "NONCE_REUSED"
    assert service.journal_entry(other).response is not None  # not deleted


def test_nonce_replay_is_refused_across_a_restart(tmp_path: Path) -> None:
    world = market_world()
    clock = Clock()
    db = tmp_path / "i.sqlite3"
    service = open_service(db, world, clock=clock, register=(GENERAL_FACILITY,))
    report = service.purchase(job(1), agent_signer)
    assert isinstance(report, PurchaseReport)
    once = authorize(service, report.operation_id, R)
    service.retrieve(once)
    service.close()
    reopened = open_service(db, world, clock=clock)
    assert refused(lambda: reopened.retrieve(once)) == "NONCE_REUSED"


def test_retrieval_authorization_cannot_acknowledge(tmp_path: Path) -> None:
    service, (op,), _ = settled(tmp_path)
    retrieve(service, op)
    as_retrieve = authorize(service, op, R)
    assert refused(lambda: service.acknowledge(as_retrieve)) == "WRONG_ACTION"
    relabelled = replace(as_retrieve, action=A)  # the signature covers the action
    assert refused(lambda: service.acknowledge(relabelled)) == "BAD_SIGNATURE"
    assert service.journal_entry(op).response is not None


def test_authorization_for_one_operation_does_not_open_another(tmp_path: Path) -> None:
    service, (first, second), _ = settled(tmp_path, jobs=2)
    for_first = authorize(service, first, R)
    moved = replace(for_first, operation_id=second)
    assert refused(lambda: service.retrieve(moved)) == "BAD_SIGNATURE"
    assert service.journal_entry(second).state is S.SETTLED


def test_expired_overlong_and_malformed_authorizations_are_refused(tmp_path: Path) -> None:
    service, (op,), _ = settled(tmp_path)
    expired = authorize(service, op, R, lifetime=-5)
    assert refused(lambda: service.retrieve(expired)) == "AUTHORIZATION_EXPIRED"
    overlong = authorize(service, op, R, lifetime=MAX_LIFETIME_SECONDS + 60)
    assert refused(lambda: service.retrieve(overlong)) == "AUTHORIZATION_TOO_LONG"
    bad_nonce = authorize(service, op, R, nonce="NOT-HEX")
    assert refused(lambda: service.retrieve(bad_nonce)) == "MALFORMED_NONCE"
    zeroed = replace(authorize(service, op, R), signature=Ed25519Signature("00" * 64))
    assert refused(lambda: service.retrieve(zeroed)) == "BAD_SIGNATURE"
    unknown = authorize(service, "op-" + "0" * 32, R)
    assert refused(lambda: service.retrieve(unknown)) == "UNKNOWN_OPERATION"


def test_statement_is_domain_separated_from_c3a_signatures() -> None:
    from inference_world import NOW

    from credence.core.signatures import SIGNATURE_DOMAIN

    assert AUTHORIZATION_DOMAIN != SIGNATURE_DOMAIN
    message = statement_bytes("op-x", R, key_id_for(AGENT_KEY.verify_key), "ab" * 16, NOW)
    assert AUTHORIZATION_DOMAIN.encode() in message[:64]
    assert SIGNATURE_DOMAIN.encode() not in message
    a = sign_response_authorization(AGENT_KEY, "op-x", R, NOW, nonce="ab" * 16)
    b = sign_response_authorization(AGENT_KEY, "op-x", A, NOW, nonce="ab" * 16)
    assert a.signature != b.signature


def test_unavailable_responses_stay_unavailable_even_when_authorized(tmp_path: Path) -> None:
    world = market_world()
    failing_db = tmp_path / "i.sqlite3"
    service = open_service(
        failing_db, world, providers=(world.local,), register=(GENERAL_FACILITY,)
    )
    report = service.purchase(job(1), agent_signer)  # chosen Nebius is not connected
    assert isinstance(report, PurchaseReport) and report.state is S.RELEASED_UNDELIVERED
    with pytest.raises(ResponseUnavailable):
        service.retrieve(authorize(service, report.operation_id, R))
