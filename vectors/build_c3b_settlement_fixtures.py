"""Generate the Credence Core C3B settlement conformance fixtures in vectors/c3b/.

Run from the repository root:  uv run python vectors/build_c3b_settlement_fixtures.py

Each fixture is a transcript of one scripted scenario run against a fresh SQLite database through
the real settlement coordinator, policy engine, verifier and C3A signatures. Purchases and
deliveries are built with the C3B test helpers in tests/unit (deterministic TEST keys, NOT FOR
PRODUCTION; Ed25519 is deterministic). Paths and wall-clock time never enter a transcript, so
regeneration is byte-identical; tests/conformance/test_core_c3b_settlement_fixtures.py checks it.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUT = HERE / "c3b"
sys.path.insert(0, str(HERE.parent / "tests" / "unit"))

from c2_world import QUOTE  # noqa: E402
from c3a_keys import ATTACKER  # noqa: E402
from c3b_world import (  # noqa: E402
    NOW,
    coordinator,
    current_facility,
    delivery,
    facility,
    purchase,
)

from credence.core.objects import Facility, Reservation  # noqa: E402
from credence.core.primitives import MicroTrlusd  # noqa: E402
from credence.core.reservation_ledger import ReservationLedger  # noqa: E402
from credence.core.settlement_coordinator import (  # noqa: E402
    FAULT_POINTS,
    DeliverySubmission,
    PurchaseReference,
    SettlementCoordinator,
    SettlementRefused,
)

ACCOUNTING_QUOTE = replace(
    QUOTE,
    input_price_per_million_micro_trlusd=MicroTrlusd(1_000_000),
    output_price_per_million_micro_trlusd=MicroTrlusd(0),
    max_input_tokens=100_000,
    max_output_tokens=0,
)


class Run:
    """One scenario on a fresh database, recording a transcript."""

    def __init__(self, tmp: Path, *, fault: Callable[[str], None] | None = None) -> None:
        self.base: Facility = facility(limit=1_000_000, drawn=0)
        self.path = tmp / "fixture.sqlite3"
        self.coord = coordinator(self.path, self.base, fault=fault)
        self.ledger = ReservationLedger(self.path)
        self.steps: list[dict[str, Any]] = []

    def reserve(self, index: int) -> tuple[Any, Reservation]:
        p = purchase(index, current_facility(self.coord, self.base), quote=ACCOUNTING_QUOTE)
        result = self.coord.reserve(p, NOW)
        assert result.reservation is not None
        # Integration I1e: settlement needs Core's persisted delivery attempt. Recording it is not
        # a transcript step, so the C3B transcripts are unchanged.
        self.coord.begin_delivery(
            PurchaseReference(
                result.reservation.reservation_id,
                p.request,
                p.quote,
                p.service_class,
                p.execution_profile,
            ),
            NOW,
        )
        self.steps.append(
            {
                "action": "reserve",
                "purchase": index,
                "policy_decision": result.decision.record.digest().value,
                "reason_code": None,
                "reservation": result.reservation.to_canonical(),
            }
        )
        return p, result.reservation

    def settle(
        self, label: str, sub: DeliverySubmission, at_offset: int = 5, at: Any = None
    ) -> None:
        when = at if at is not None else NOW.plus_seconds(at_offset)
        step: dict[str, Any] = {"action": "settle", "label": label, "at": when.value}
        try:
            result = self.coord.settle(sub, when)
        except SettlementRefused as exc:
            step["refused"] = exc.reason
        except RuntimeError as exc:  # injected fault
            step["fault"] = str(exc)
        else:
            record = result.verifier_record
            step.update(
                {
                    "outcome": result.outcome.value,
                    "verifier_decision": record.digest().value,
                    "reason_code": None if record.reason_code is None else record.reason_code.value,
                    "actual_amount_micro_trlusd": (
                        None if result.actual_amount is None else result.actual_amount.value
                    ),
                    "reservation_status": result.reservation_status.value,
                }
            )
        step["receipt"] = sub.receipt.digest().value
        self.steps.append(step)

    def state(self, reservations: list[Reservation]) -> dict[str, Any]:
        exposure = self.coord.exposure(self.base.facility_id, NOW)
        head = self.coord.chain_head(self.base.facility_id)
        return {
            "limit_micro_trlusd": exposure.limit.value,
            "drawn_micro_trlusd": exposure.drawn.value,
            "held_micro_trlusd": exposure.held.value,
            "headroom_micro_trlusd": exposure.headroom,
            "chain_head": None if head is None else head.value,
            "reservations": {
                r.reservation_id.value: {
                    "status": self.ledger.get(r.reservation_id).status.value,
                    "release_reason": self.ledger.release_reason(r.reservation_id),
                }
                for r in reservations
            },
            "disputes": [
                dict(
                    zip(
                        (
                            "kind",
                            "reservation_id",
                            "facility_id",
                            "receipt_digest",
                            "verifier_decision_digest",
                            "reason_code",
                            "recorded_at",
                        ),
                        row,
                        strict=True,
                    )
                )
                for row in self.coord.disputes()
            ],
        }


def successful(tmp: Path) -> dict[str, Any]:
    run = Run(tmp)
    p, r = run.reserve(1)
    run.settle("verified delivery, 60,000 of 100,000", delivery(
        p, r.reservation_id, previous=None, input_tokens=60_000, output_tokens=0
    ))  # fmt: skip
    return {"steps": run.steps, "final": run.state([r])}


def rejected(tmp: Path) -> dict[str, Any]:
    run = Run(tmp)
    p1, r1 = run.reserve(1)
    run.settle("forged gateway signature (unauthenticated)", delivery(
        p1, r1.reservation_id, previous=None, input_tokens=60_000, output_tokens=0,
        gateway=ATTACKER,
    ))  # fmt: skip
    p2, r2 = run.reserve(2)
    run.settle("gateway-signed over-billed receipt (authenticated terminal)", delivery(
        p2, r2.reservation_id, previous=None, input_tokens=60_000, output_tokens=0,
        price_delta=1,
    ))  # fmt: skip
    return {"steps": run.steps, "final": run.state([r1, r2])}


def duplicate(tmp: Path) -> dict[str, Any]:
    run = Run(tmp)
    p1, r1 = run.reserve(1)
    sub = delivery(p1, r1.reservation_id, previous=None, input_tokens=60_000, output_tokens=0)
    run.settle("first settlement", sub)
    run.settle("same reservation again", sub, at_offset=6)
    p2, r2 = run.reserve(2)
    run.settle(
        "same receipt against another reservation",
        replace(sub, reservation_id=r2.reservation_id, request=p2.request),
        at_offset=7,
    )
    return {"steps": run.steps, "final": run.state([r1, r2])}


def expiration(tmp: Path) -> dict[str, Any]:
    run = Run(tmp)
    p, r = run.reserve(1)
    sub = delivery(p, r.reservation_id, previous=None, input_tokens=60_000, output_tokens=0)
    run.settle("at expires_at", sub, at=r.expires_at)
    run.settle("delayed receipt after expiry", sub, at=r.expires_at.plus_seconds(-1))
    return {"steps": run.steps, "final": run.state([r])}


def rollback(tmp: Path) -> dict[str, Any]:
    cases = []
    for point in FAULT_POINTS:

        def fault(at: str, point: str = point) -> None:
            if at == point:
                raise RuntimeError(f"injected at {at}")

        run = Run(tmp / point, fault=fault)
        p, r = run.reserve(1)
        sub = delivery(p, r.reservation_id, previous=None, input_tokens=60_000, output_tokens=0)
        run.settle(f"fault {point}", sub)
        after_fault = run.state([r])
        healthy = SettlementCoordinator(run.ledger, run.coord._registry)
        run.coord = healthy
        run.settle("retry without fault", sub, at_offset=6)
        cases.append(
            {"fault_point": point, "steps": run.steps, "after_fault": after_fault,
             "final": run.state([r])}
        )  # fmt: skip
    return {"cases": cases}


SCENARIOS: list[tuple[str, str, Callable[[Path], dict[str, Any]]]] = [
    ("successful-settlement", "Reserve 100,000, settle a verified 60,000 delivery", successful),
    ("rejected-settlement", "Unauthenticated vs authenticated-terminal rejections", rejected),
    ("duplicate-settlement", "Same reservation twice; same receipt elsewhere", duplicate),
    ("expiration", "Settlement at and after expires_at", expiration),
    ("rollback", "A failure at each transition point leaves no partial state", rollback),
]


def render() -> dict[str, str]:
    files: dict[str, str] = {}
    manifest: list[list[str]] = []
    for fid, description, scenario in SCENARIOS:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for point in FAULT_POINTS:
                (root / point).mkdir()
            body = scenario(root)
        text = json.dumps({"id": fid, "description": description, **body}, indent=2) + "\n"
        name = f"{fid}.json"
        files[name] = text
        manifest.append([hashlib.sha256(text.encode("ascii")).hexdigest(), name])
    manifest.sort(key=lambda pair: pair[1])
    files["MANIFEST.json"] = json.dumps(manifest, indent=1) + "\n"
    return files


def main() -> int:
    files = render()
    OUT.mkdir(exist_ok=True)
    stale = sorted({p.name for p in OUT.glob("*.json")} - set(files))
    if stale:
        raise SystemExit(f"vectors/c3b has files the generator does not produce: {stale}")
    for name, text in files.items():
        (OUT / name).write_text(text, encoding="ascii")
    print(f"wrote {len(files) - 1} settlement fixtures to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
