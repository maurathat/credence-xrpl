"""Child process for the I1e subprocess crash tests. Not a test module.

Usage: python i1_child.py <database> <scenario>

Runs one integrated purchase against the offline market world (fake providers, TEST keys, NOT FOR
PRODUCTION) and stops at the scenario's point: it prints a marker line and then blocks until the
parent kills it with SIGKILL, so no cleanup runs, as in a real crash.

Scenarios: provider (inside the provider call, after it executed), attempt (after begin_delivery,
before the provider call), evidence (evidence stored, before settlement), retrieved (settled and
retrieved, before acknowledgment).
"""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "unit"), str(HERE.parents[1])]

from i1_world import (  # noqa: E402
    GENERAL_FACILITY,
    Wrapped,
    agent_signer,
    job,
    market_world,
    open_service,
    retrieve,
)

from credence.inference.provider import ProviderRequest, ProviderResult  # noqa: E402


def block(marker: str) -> None:
    print(marker, flush=True)
    while True:
        time.sleep(60)


class BlockAfterExecuting(Wrapped):
    def execute(self, request: ProviderRequest) -> ProviderResult:
        super().execute(request)  # the provider has executed (chargeable)
        block("IN_PROVIDER")
        raise AssertionError("unreachable")


def check_revoked(database: Path) -> None:
    """A fresh process with a freshly built registry listing every key as current: report
    whether the agent can retrieve, acknowledge and purchase. Prints one JSON line."""
    import json

    from i1_world import Clock, acknowledge
    from inference_world import NOW

    from credence.integration.service import operation_id_for

    world = market_world()
    service = open_service(database, world, clock=Clock(NOW.plus_seconds(120)))
    op = operation_id_for("job-facility-market-general-1")
    out: dict[str, str] = {}
    for name, call in (
        ("retrieve", lambda: retrieve(service, op)),
        ("acknowledge", lambda: acknowledge(service, op)),
    ):
        try:
            call()
            out[name] = "allowed"
        except Exception as exc:  # reported, not raised
            out[name] = f"refused {exc}"
    report = service.purchase(job(2), agent_signer)
    out["purchase"] = f"{report.state.value} {getattr(report, 'detail', '')}".strip()  # type: ignore[union-attr]
    out["provider_calls"] = str(len(world.nebius_transport.posts))
    print(json.dumps(out), flush=True)


def main(database: Path, scenario: str) -> None:
    if scenario == "check_revoked":
        check_revoked(database)
        return
    world = market_world()
    providers = None
    hook = None
    if scenario == "provider":
        providers = (BlockAfterExecuting(world.nebius), world.local)
    elif scenario in ("attempt", "evidence"):
        point = {"attempt": "after_attempt_begun", "evidence": "after_evidence_stored"}[scenario]

        def hook(reached: str) -> None:
            if reached == point:
                block(f"AT_{point.upper()}")

    service = open_service(
        database, world, providers=providers, register=(GENERAL_FACILITY,), crash_hook=hook
    )
    report = service.purchase(job(1), agent_signer)
    if scenario == "retrieved":
        response = retrieve(service, report.operation_id)  # type: ignore[union-attr]
        block("RETRIEVED " + hashlib.sha256(response).hexdigest())
    raise AssertionError(f"scenario {scenario} did not stop: {report}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
