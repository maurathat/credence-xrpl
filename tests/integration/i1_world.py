"""Integration I1 test world: the IntegrationService over Inference's offline market world.

Offline and deterministic: fake Nebius transport, fake signing local provider, TEST keys (NOT FOR
PRODUCTION). No network, model or XRPL.

Owner: Integration (San Antonio).
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))

from inference_world import AGENT_KEY, FAR_FUTURE, GATEWAY_KEY, NOW, prompt  # noqa: E402
from market_world import (  # noqa: E402
    CONTROLLED,
    CONTROLLED_FACILITY,
    GENERAL,
    GENERAL_FACILITY,
    LOCAL_PROFILE,
    LOCAL_QUOTE,
    LOCAL_SERVICE,
    NEBIUS_PROFILE,
    NEBIUS_QUOTE,
    NEBIUS_SERVICE,
    MarketWorld,
    market_world,
)

from credence.core.objects import (  # noqa: E402
    Facility,
    ProcurementClass,
    PurchaseRequest,
    SignedObject,
)
from credence.core.primitives import MicroTrlusd, ProviderId, Timestamp  # noqa: E402
from credence.core.signatures import key_id_for, sign  # noqa: E402
from credence.inference.provider import (  # noqa: E402
    AttestationCapability,
    InferenceProvider,
    ProviderRequest,
    ProviderResult,
)
from credence.integration.service import IntegrationService, Job, Offer  # noqa: E402

__all__ = [
    "CONTROLLED",
    "CONTROLLED_FACILITY",
    "GENERAL",
    "GENERAL_FACILITY",
    "NOW",
    "market_world",
]

OFFERS = (
    Offer(NEBIUS_QUOTE, NEBIUS_SERVICE, NEBIUS_PROFILE),
    Offer(LOCAL_QUOTE, LOCAL_SERVICE, LOCAL_PROFILE),
)
AGENT_KEY_ID = key_id_for(AGENT_KEY.verify_key).value
OTHER_KEY_ID = key_id_for(GATEWAY_KEY.verify_key).value  # registered, but not the agent's key
TTL_SECONDS = 600


def agent_signer(request: PurchaseRequest) -> SignedObject:
    return sign(request, AGENT_KEY)


def job(
    index: int,
    facility: Facility = GENERAL_FACILITY,
    procurement: ProcurementClass = GENERAL,
    *,
    key: str | None = None,
) -> Job:
    return Job(
        idempotency_key=key or f"job-{facility.facility_id.value}-{index}",
        facility=facility,
        procurement_classes=(procurement,),
        offers=OFFERS,
        prompt=prompt(index),
        request_expires_at=FAR_FUTURE,
    )


class Clock:
    """Deterministic, thread-safe clock: each call returns the next whole second."""

    def __init__(self, start: Timestamp = NOW) -> None:
        self._next = start
        self._lock = threading.Lock()

    def __call__(self) -> Timestamp:
        with self._lock:
            current = self._next
            self._next = current.plus_seconds(1)
            return current

    def advance(self, seconds: int) -> None:
        with self._lock:
            self._next = self._next.plus_seconds(seconds)


class SimulatedCrash(BaseException):
    """Stands in for process death: not an Exception, so no handler in the service catches it."""


def crash_at(point: str) -> Callable[[str], None]:
    def hook(reached: str) -> None:
        if reached == point:
            raise SimulatedCrash(point)

    return hook


class Wrapped:
    """An InferenceProvider wrapper that counts executions; subclasses change `execute`."""

    def __init__(self, inner: InferenceProvider) -> None:
        self.inner = inner
        self.calls = 0

    @property
    def provider_id(self) -> ProviderId:
        return self.inner.provider_id

    @property
    def attestation_capability(self) -> AttestationCapability:
        return self.inner.attestation_capability

    def execute(self, request: ProviderRequest) -> ProviderResult:
        self.calls += 1
        return self.inner.execute(request)


class CrashDuringExecution(Wrapped):
    """The provider executes (chargeable), then the process dies before the result returns."""

    def execute(self, request: ProviderRequest) -> ProviderResult:
        super().execute(request)
        raise SimulatedCrash("provider outstanding")


class ClockJump(Wrapped):
    """Delivery outlives the reservation: the clock jumps while the provider runs."""

    def __init__(self, inner: InferenceProvider, clock: Clock, seconds: int) -> None:
        super().__init__(inner)
        self._clock = clock
        self._seconds = seconds

    def execute(self, request: ProviderRequest) -> ProviderResult:
        result = super().execute(request)
        self._clock.advance(self._seconds)
        return result


class ConcurrencyMeter(Wrapped):
    """Records the most executions in flight at once, holding each one briefly."""

    def __init__(self, inner: InferenceProvider) -> None:
        super().__init__(inner)
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def execute(self, request: ProviderRequest) -> ProviderResult:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            threading.Event().wait(0.05)
            return super().execute(request)
        finally:
            with self._lock:
                self._active -= 1


class WriteLockProbe(Wrapped):
    """During execute, tries to take the database's SQLite write lock without waiting."""

    def __init__(self, inner: InferenceProvider, db: Path) -> None:
        super().__init__(inner)
        self._db = db
        self.write_lock_free_during_execute: list[bool] = []

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
        return super().execute(request)


def open_service(
    db: Path,
    world: MarketWorld,
    *,
    providers: Sequence[InferenceProvider] | None = None,
    clock: Clock | None = None,
    register: Sequence[Facility] = (),
    recover: bool = True,
    crash_hook: Callable[[str], None] | None = None,
    coordinator_fault: Callable[[str], None] | None = None,
) -> IntegrationService:
    service = IntegrationService(
        db,
        registry=world.registry(),
        gateway_key=GATEWAY_KEY,
        providers=(world.nebius, world.local) if providers is None else providers,
        clock=Clock() if clock is None else clock,
        recover=recover,
        crash_hook=crash_hook,
        coordinator_fault=coordinator_fault,
    )
    for facility in register:
        service.register_facility(facility)
    return service


def stale(facility: Facility) -> Facility:
    """The facility as a caller might still present it: drawn 0."""
    return replace(facility, drawn_micro_trlusd=MicroTrlusd(0))


def database_contains(db: Path, needle: bytes) -> bool:
    """Whether any text or blob cell in any table contains `needle`."""
    with sqlite3.connect(db) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            for row in conn.execute(f'SELECT * FROM "{table}"'):
                for cell in row:
                    raw = cell.encode() if isinstance(cell, str) else cell
                    if isinstance(raw, bytes) and needle in raw:
                        return True
    return False


def count(db: Path, sql: str) -> int:
    with sqlite3.connect(db) as conn:
        return int(conn.execute(sql).fetchone()[0])
