"""Integration I1b world: the Inference gateway and the Core settlement coordinator on one ledger.

Offline and deterministic. It reuses Inference's market world (fake Nebius transport, fake signing
local provider, TEST keys, NOT FOR PRODUCTION) and wires it to Core's C3B coordinator over one
shared reservation-ledger SQLite file. This is test wiring only. It contains no production glue;
that is Integration I1c.

No network, no model, no XRPL.

Owner: Integration (San Antonio).
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))

from inference_world import GATEWAY_KEY, NOW, StepClock  # noqa: E402
from market_world import (  # noqa: E402
    CONTROLLED,
    CONTROLLED_FACILITY,
    GENERAL,
    GENERAL_FACILITY,
    NEBIUS_MODEL,
    MarketWorld,
    candidates,
    market_world,
)

from credence.core.objects import Facility, ProcurementClass, Reservation  # noqa: E402
from credence.core.primitives import FacilityId, ProviderId, Sha256Digest, Timestamp  # noqa: E402
from credence.core.reservation_ledger import ReservationLedger  # noqa: E402
from credence.core.settlement_coordinator import (  # noqa: E402
    DeliverySubmission,
    SettlementCoordinator,
)
from credence.inference.gateway import (  # noqa: E402
    DeliveryEvidence,
    DeliveryVerified,
    Gateway,
    Purchase,
)
from credence.inference.payload import payload_digest  # noqa: E402
from credence.inference.provider import (  # noqa: E402
    AttestationCapability,
    InferenceProvider,
    ProviderRequest,
    ProviderResult,
)
from credence.inference.receipt_state import ReceiptChain  # noqa: E402

__all__ = [
    "CONTROLLED",
    "CONTROLLED_FACILITY",
    "GENERAL",
    "GENERAL_FACILITY",
    "NOW",
    "market_world",
]

# Reservations are created from NOW by the gateway's StepClock and live 600 s (Core default TTL).
SETTLE_AT = Timestamp("2026-10-24T15:01:00Z")
AFTER_EXPIRY = Timestamp("2026-10-24T15:20:00Z")


@dataclass
class Rig:
    world: MarketWorld
    db: Path
    ledger: ReservationLedger
    coordinator: SettlementCoordinator
    gateway: Gateway

    def new_gateway(self, *, start: Timestamp) -> Gateway:
        """A second gateway process on the same database: fresh in-memory receipt chain."""
        return Gateway(
            ledger=self.ledger,
            registry=self.world.registry(),
            gateway_key=GATEWAY_KEY,
            providers=(self.world.nebius, self.world.local),
            clock=StepClock(start),
        )

    def refreshed(self, base: Facility, job: int, procurement: ProcurementClass) -> list[Purchase]:
        """Market candidates for `job`, presenting the facility's stored drawn amount."""
        drawn = self.coordinator.exposure(base.facility_id, NOW).drawn
        current = replace(base, drawn_micro_trlusd=drawn)
        return [replace(c, facility=current) for c in candidates(base, procurement, job)]

    def table_names(self) -> set[str]:
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {str(r[0]) for r in rows}

    def database_contains(self, needle: bytes) -> bool:
        """Whether any text or blob cell in any table contains `needle`."""
        with sqlite3.connect(self.db) as conn:
            for table in self.table_names():
                for row in conn.execute(f'SELECT * FROM "{table}"'):
                    for cell in row:
                        raw = cell.encode() if isinstance(cell, str) else cell
                        if isinstance(raw, bytes) and needle in raw:
                            return True
        return False


def rig(
    tmp_path: Path,
    *,
    world: MarketWorld | None = None,
    providers: Sequence[InferenceProvider] | None = None,
    clock: Callable[[], Timestamp] | None = None,
    chain: ReceiptChain | None = None,
    register: Sequence[Facility] = (),
) -> Rig:
    world = market_world() if world is None else world
    db = tmp_path / "reservations.sqlite3"
    ledger = ReservationLedger(db)
    coordinator = SettlementCoordinator(ledger, world.registry())
    for facility in register:
        coordinator.register_facility(facility)
    gateway = Gateway(
        ledger=ledger,
        registry=world.registry(),
        gateway_key=GATEWAY_KEY,
        providers=(world.nebius, world.local) if providers is None else providers,
        clock=StepClock() if clock is None else clock,
        chain=chain,
    )
    return Rig(world, db, ledger, coordinator, gateway)


def submission(
    reservation: Reservation, evidence: DeliveryEvidence, delivered: Sha256Digest
) -> DeliverySubmission:
    """The coordinator's input for one gateway delivery. Field for field; no facts computed."""
    return DeliverySubmission(
        reservation_id=reservation.reservation_id,
        receipt=evidence.receipt,
        gateway_signature=evidence.gateway_signature,
        provider_attestation=evidence.provider_attestation,
        provider_signature=evidence.provider_signature,
        delivered_response_digest=delivered,
        request=evidence.request,
        quote=evidence.quote,
        service_class=evidence.service_class,
        execution_profile=evidence.execution_profile,
    )


def verified_submission(outcome: DeliveryVerified) -> DeliverySubmission:
    return submission(outcome.reservation, outcome.evidence, payload_digest(outcome.response))


def nebius_reply(prompt_tokens: int, completion_tokens: int) -> dict[str, object]:
    """The documented non-streaming chat-completion shape with chosen usage counts (offline)."""
    return {
        "id": "chatcmpl-synthetic",
        "object": "chat.completion",
        "created": 0,
        "model": NEBIUS_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Synthetic Nebius-shaped reply."},
                "finish_reason": "stop",
                "logprobs": None,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "service_tier": "default",
    }


class LockedClock:
    """A StepClock safe to share between threads."""

    def __init__(self) -> None:
        self._clock = StepClock()
        self._lock = threading.Lock()

    def __call__(self) -> Timestamp:
        with self._lock:
            return self._clock()


class RendezvousProvider:
    """Delegates to `inner`, but each execute waits until `parties` executions are in flight."""

    def __init__(self, inner: InferenceProvider, parties: int) -> None:
        self._inner = inner
        self._barrier = threading.Barrier(parties)

    @property
    def provider_id(self) -> ProviderId:
        return self._inner.provider_id

    @property
    def attestation_capability(self) -> AttestationCapability:
        return self._inner.attestation_capability

    def execute(self, request: ProviderRequest) -> ProviderResult:
        self._barrier.wait(timeout=10)
        return self._inner.execute(request)


class RendezvousChain(ReceiptChain):
    """A ReceiptChain whose `advance` waits until `parties` gateway calls have verified, then lets
    them advance one at a time. Forces the interleaving where both verify before either advances."""

    def __init__(self, parties: int) -> None:
        super().__init__()
        self._barrier = threading.Barrier(parties)
        self._lock = threading.Lock()

    def advance(
        self, facility_id: FacilityId, previous: Sha256Digest | None, receipt: Sha256Digest
    ) -> None:
        self._barrier.wait(timeout=10)
        with self._lock:
            super().advance(facility_id, previous, receipt)


class WriteLockProbe:
    """Delegates to `inner`; during execute, tries to take the ledger's SQLite write lock with no
    wait, and records whether it could. True means no write transaction spans the provider call."""

    def __init__(self, inner: InferenceProvider, db: Path) -> None:
        self._inner = inner
        self._db = db
        self.write_lock_free_during_execute: list[bool] = []

    @property
    def provider_id(self) -> ProviderId:
        return self._inner.provider_id

    @property
    def attestation_capability(self) -> AttestationCapability:
        return self._inner.attestation_capability

    def execute(self, request: ProviderRequest) -> ProviderResult:
        conn = sqlite3.connect(self._db, timeout=0, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            self.write_lock_free_during_execute.append(True)
        except sqlite3.OperationalError:
            self.write_lock_free_during_execute.append(False)
        finally:
            conn.close()
        return self._inner.execute(request)
