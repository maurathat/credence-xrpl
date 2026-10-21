"""Durable purchase preparations for remote agent signing (Integration I2a, CR-1).

A preparation is the exact, immutable `PurchaseRequest` the service selected under Core's policy for
one authenticated agent, one facility and one offer, before the agent has signed it. The agent
signs the request's existing C3A statement (`credence.core.signatures.signature_message`) outside
the server; the service holds no agent key.

Operational, not canonical: no frozen Core object, canonical format or C3A signature definition is
changed. One row per preparation, in the same SQLite database as the journal and Core, written once
and never updated. What is kept: an opaque single-use preparation identity, the idempotency key and
operation it would create, the agent key and subject it is bound to, the facility, a digest of the
market it was selected from (facility terms, procurement classes and every offer, in order), the
chosen offer's position, the exact canonical request and approved objects, the prompt digest, and
the creation and expiry times. Prompts, signatures and private keys are never stored here.

Single use: a preparation names one idempotency key, and the journal admits one operation per key,
so a preparation can be purchased at most once. Resubmitting it returns the journaled operation.

Owner: Integration (San Antonio). Provisional (I2a): subject to Integration and security review.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from credence.core.kessai import canonicalize
from credence.core.objects import (
    ExecutionProfile,
    Facility,
    ProcurementClass,
    PurchaseRequest,
    Quote,
    ServiceClass,
)
from credence.core.primitives import FacilityId, KeyId, Sha256Digest, Timestamp

PREPARATION_TTL_SECONDS = 300
MARKET_DIGEST_PROTOCOL = "credence.integration.prepared_market"

SCHEMA = """
CREATE TABLE IF NOT EXISTS integration_preparations (
    preparation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    agent_key_id TEXT NOT NULL,
    agent_subject TEXT NOT NULL,
    facility_id TEXT NOT NULL,
    market_digest TEXT NOT NULL,
    offer_index INTEGER NOT NULL CHECK (offer_index >= 0),
    request BLOB NOT NULL,
    request_digest TEXT NOT NULL,
    prompt_digest TEXT NOT NULL,
    quote BLOB NOT NULL,
    service_class BLOB NOT NULL,
    execution_profile BLOB NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS integration_preparations_by_agent
    ON integration_preparations (agent_key_id, created_at);
"""

_COLUMNS = (
    "preparation_id",
    "idempotency_key",
    "operation_id",
    "agent_key_id",
    "agent_subject",
    "facility_id",
    "market_digest",
    "offer_index",
    "request",
    "request_digest",
    "prompt_digest",
    "quote",
    "service_class",
    "execution_profile",
    "created_at",
    "expires_at",
)


class PreparationCorrupt(RuntimeError):
    """A stored preparation does not match its own recorded digest. Fail closed."""


def new_preparation_id() -> str:
    """An opaque, unguessable preparation identity (128 random bits)."""
    return "prep-" + secrets.token_hex(16)


def market_digest(
    facility: Facility,
    procurement_classes: Sequence[ProcurementClass],
    offers: Sequence[tuple[Quote, ServiceClass, ExecutionProfile]],
) -> Sha256Digest:
    """Digest of the market a request was selected from: the facility terms as presented, its
    procurement classes and every offer, in order (Kessai serializer: JCS (RFC 8785) + NFC).

    Drawn is taken from Core at purchase time and is excluded here (the presented facility is
    hashed with drawn as configured), so settlement on the facility alone does not stale it; the
    policy re-check at submission covers exposure."""
    form = canonicalize(
        {
            "protocol": MARKET_DIGEST_PROTOCOL,
            "facility": facility.digest().value,
            "procurement_classes": [pc.digest().value for pc in procurement_classes],
            "offers": [
                [quote.digest().value, service.digest().value, profile.digest().value]
                for quote, service, profile in offers
            ],
        }
    )
    return Sha256Digest.from_hex(form.sha256_hex)


@dataclass(frozen=True)
class Preparation:
    """One stored preparation. `request` is the exact request the agent must sign."""

    preparation_id: str
    idempotency_key: str
    operation_id: str
    agent_key_id: KeyId
    agent_subject: str
    facility_id: FacilityId
    market_digest: Sha256Digest
    offer_index: int
    request: PurchaseRequest
    quote: Quote
    service_class: ServiceClass
    execution_profile: ExecutionProfile
    prompt_digest: Sha256Digest
    created_at: Timestamp
    expires_at: Timestamp

    @property
    def request_digest(self) -> Sha256Digest:
        return self.request.digest()


def _decode_row(row: tuple[object, ...]) -> Preparation:
    data = dict(zip(_COLUMNS, row, strict=True))

    def blob(name: str) -> object:
        value = data[name]
        if not isinstance(value, bytes):
            raise PreparationCorrupt(f"{data['preparation_id']}: {name} is not canonical bytes")
        return json.loads(value)

    request = PurchaseRequest.from_canonical(blob("request"))
    if request.digest().value != data["request_digest"]:
        raise PreparationCorrupt(f"{data['preparation_id']}: request digest mismatch")
    return Preparation(
        preparation_id=str(data["preparation_id"]),
        idempotency_key=str(data["idempotency_key"]),
        operation_id=str(data["operation_id"]),
        agent_key_id=KeyId(str(data["agent_key_id"])),
        agent_subject=str(data["agent_subject"]),
        facility_id=FacilityId(str(data["facility_id"])),
        market_digest=Sha256Digest(str(data["market_digest"])),
        offer_index=int(str(data["offer_index"])),
        request=request,
        quote=Quote.from_canonical(blob("quote")),
        service_class=ServiceClass.from_canonical(blob("service_class")),
        execution_profile=ExecutionProfile.from_canonical(blob("execution_profile")),
        prompt_digest=Sha256Digest(str(data["prompt_digest"])),
        created_at=Timestamp(str(data["created_at"])),
        expires_at=Timestamp(str(data["expires_at"])),
    )


class PreparationStore:
    def __init__(self, path: Path, *, busy_timeout: float = 30) -> None:
        self._path = str(path)
        self._busy_timeout = busy_timeout
        with self._write() as conn:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)

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

    def insert(self, preparation: Preparation) -> None:
        values = (
            preparation.preparation_id,
            preparation.idempotency_key,
            preparation.operation_id,
            preparation.agent_key_id.value,
            preparation.agent_subject,
            preparation.facility_id.value,
            preparation.market_digest.value,
            preparation.offer_index,
            preparation.request.canonical_bytes(),
            preparation.request.digest().value,
            preparation.prompt_digest.value,
            preparation.quote.canonical_bytes(),
            preparation.service_class.canonical_bytes(),
            preparation.execution_profile.canonical_bytes(),
            preparation.created_at.value,
            preparation.expires_at.value,
        )
        with self._write() as conn:
            conn.execute(
                f"INSERT INTO integration_preparations ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                values,
            )

    def get(self, preparation_id: str) -> Preparation | None:
        with self._write() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM integration_preparations "
                "WHERE preparation_id = ?",
                (preparation_id,),
            ).fetchone()
        return None if row is None else _decode_row(tuple(row))
