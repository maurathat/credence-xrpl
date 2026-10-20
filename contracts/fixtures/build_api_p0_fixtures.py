"""Generate the Product P0 offline operation transcripts (no network, no model, no XRPL).

Run from the repository root:  uv run python contracts/fixtures/build_api_p0_fixtures.py

CONTROLLED OFFLINE DATA. Each transcript is one real run of `IntegrationService` (baseline
a3ec0a3) over the Integration I1 offline test world (tests/integration/i1_world.py): the real
NebiusProvider adapter over a FAKE transport returning a synthetic reply, the FAKE signing provider
standing in for local Qwen, TEST keys (NOT FOR PRODUCTION), a deterministic clock and a temporary
SQLite file. Nothing here is a Nebius response, a local Qwen attestation or a payment. The
responses are synthetic and the Tier 2 provider signature is made with a TEST key.

Snapshots are taken at each of the service's interruption points by a hook that records and never
raises, and after each public call. Each snapshot reads only the journal and Core's read-only
AccountingView. Interrupted runs use the I1 harness's simulated crash and then the service's own
recovery. The disputed run forces CHAIN_BROKEN the way the I1 test does, by moving Core's chain
head out of band; that is a test-harness manipulation and is labelled as one.

Accounting settlement is not XRPL settlement: no XRPL transaction exists in any transcript.
Amounts are integer micro-tRLUSD (tRLUSD is a test token).

tests/conformance/test_api_p0_fixtures.py regenerates this file byte-for-byte.

Owner: API/UI.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "api_p0" / "offline-operations.json"
sys.path.insert(0, str(ROOT / "tests" / "integration"))
sys.path.insert(0, str(ROOT / "tests" / "unit"))

from i1_world import (  # noqa: E402
    AGENT_KEY,
    CONTROLLED,
    CONTROLLED_FACILITY,
    GENERAL,
    GENERAL_FACILITY,
    TTL_SECONDS,
    Clock,
    ClockJump,
    SimulatedCrash,
    acknowledge,
    agent_signer,
    job,
    market_world,
    open_service,
    retrieve,
)
from inference_world import GATEWAY_KEY, GATEWAY_SUBJECT  # noqa: E402
from market_world import LOCAL_TEST_KEY, MarketWorld  # noqa: E402

from credence.core.objects import CanonicalObject, Facility, ProcurementClass  # noqa: E402
from credence.core.primitives import FacilityId, ReservationId, Sha256Digest  # noqa: E402
from credence.core.reservation_ledger import ReservationStateError  # noqa: E402
from credence.core.signatures import key_id_for  # noqa: E402
from credence.inference.local_llama import LOCAL_QWEN_PROVIDER_ID  # noqa: E402
from credence.inference.nebius import NEBIUS_PROVIDER_ID  # noqa: E402
from credence.integration.journal import RESPONSE_AVAILABLE, Operation  # noqa: E402
from credence.integration.service import (  # noqa: E402
    CRASH_POINTS,
    IntegrationService,
    PurchaseRefused,
    operation_id_for,
)


def _c(obj: CanonicalObject | None) -> Any:
    return None if obj is None else obj.to_canonical()


class Recorder:
    """Collects snapshots of one operation. The hook records and never raises, unless the run
    asks it to simulate a crash at one point (after recording it)."""

    def __init__(self, facility: Facility, idempotency_key: str, clock: Clock) -> None:
        self.facility_id = facility.facility_id
        self.operation_id = operation_id_for(idempotency_key)
        self.clock = clock
        self.service: IntegrationService | None = None
        self.crash_point: str | None = None
        self.steps: list[dict[str, Any]] = []

    def hook(self, point: str) -> None:
        self.snapshot(point, "interruption point reached")
        if point == self.crash_point:
            raise SimulatedCrash(point)

    def snapshot(self, step: str, note: str, **extra: Any) -> None:
        assert self.service is not None
        self.steps.append(snapshot(self.service, self.facility_id, self.operation_id, self.clock))
        self.steps[-1] = {"step": step, "note": note, **self.steps[-1], **extra}


def snapshot(
    service: IntegrationService, facility_id: FacilityId, operation_id: str, clock: Clock
) -> dict[str, Any]:
    now = clock.peek()
    exposure = service.accounting.exposure(facility_id, now)
    op: Operation | None
    try:
        op = service.journal_entry(operation_id)
    except KeyError:
        op = None
    reservation_status = None
    release_reason = None
    attempt = None
    if op is not None and op.reservation_id is not None:
        rid = ReservationId(op.reservation_id)
        try:
            reservation_status = service.accounting.reservation(rid).status.value
            release_reason = service.accounting.release_reason(rid)
        except ReservationStateError:
            reservation_status = None
        recorded = service.accounting.delivery_attempt(rid)
        if recorded is not None:
            attempt = {
                "begun_at": recorded.begun_at.value,
                "execution_claimed_at": None
                if recorded.execution_claimed_at is None
                else recorded.execution_claimed_at.value,
                "unknown_cause": None
                if recorded.unknown_cause is None
                else recorded.unknown_cause.value,
            }
    head = service.accounting.chain_head(facility_id)
    return {
        "at": now.value,
        "journal_state": None if op is None else op.state.value,
        "core": {
            "exposure": {
                "limit_micro_trlusd": exposure.limit.value,
                "drawn_micro_trlusd": exposure.drawn.value,
                "held_micro_trlusd": exposure.held.value,
                "headroom_micro_trlusd": exposure.headroom,
            },
            "reservation_status": reservation_status,
            "release_reason": release_reason,
            "delivery_attempt": attempt,
            "chain_head": None if head is None else head.value,
            "receipt_accounting_settled": None
            if op is None or op.receipt_digest is None
            else service.accounting.receipt_settled(Sha256Digest(op.receipt_digest)),
        },
        # Bytes kept in the journal, and whether the state releases them to the authorized agent.
        "response_retained": op is not None and op.response is not None,
        "response_available_to_agent": op is not None and op.state in RESPONSE_AVAILABLE,
    }


def operation_record(service: IntegrationService, operation_id: str) -> dict[str, Any]:
    op = service.journal_entry(operation_id)
    return {
        "operation_id": op.operation_id,
        "idempotency_key": op.idempotency_key,
        "facility_id": op.facility_id,
        "prompt_digest": op.prompt_digest,
        "state": op.state.value,
        "detail": op.detail,
        "provider_id": op.provider_id,
        "reservation_id": op.reservation_id,
        "request_digest": op.row["request_digest"],
        "agent_key_id": op.agent_key_id,
        "agent_subject": op.agent_subject,
        "receipt_digest": op.receipt_digest,
        "response_digest": op.response_digest,
        "settlement_outcome": op.settlement_outcome,
        "verifier_decision_digest": op.row["verifier_decision_digest"],
        "reason_code": op.reason_code,
        "created_at": op.row["created_at"],
        "updated_at": op.row["updated_at"],
        "retrieved_at": op.retrieved_at,
        "acknowledged_at": op.acknowledged_at,
        "objects": {
            "purchase_request": _c(op.request()),
            "purchase_request_signature": _c(op.request_signature()),
            "quote": _c(op.quote()),
            "service_class": _c(op.service_class()),
            "execution_profile": _c(op.execution_profile()),
            "receipt": _c(op.receipt()),
            "gateway_signature": _c(op.gateway_signature()),
            "provider_attestation": _c(op.provider_attestation()),
            "provider_signature": _c(op.provider_signature()),
        },
    }


def core_records(service: IntegrationService, facility_id: FacilityId) -> dict[str, Any]:
    fid = facility_id.value
    return {
        "settlement_disputes": [
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
            for row in service.accounting.disputes()
            if row[2] == fid
        ],
        "late_delivery_disputes": [
            {
                "receipt_digest": d.receipt_digest.value,
                "reservation_id": d.reservation_id.value,
                "expires_at": d.expires_at.value,
                "submitted_at": d.submitted_at.value,
            }
            for d in service.accounting.late_delivery_disputes()
            if d.facility_id.value == fid
        ],
        "no_delivery_releases": [
            {
                "reservation_id": r.reservation_id.value,
                "reason": r.reason.value,
                "released_at": r.released_at.value,
            }
            for r in service.accounting.no_delivery_releases()
            if r.facility_id.value == fid
        ],
    }


def registry_record() -> list[dict[str, str]]:
    """The public half of the TEST keys the runs trust: key ID (the public key), role, subject."""
    return [
        {
            "key_id": key_id_for(AGENT_KEY.verify_key).value,
            "role": "agent",
            "subject": GENERAL_FACILITY.borrower_account.value,
        },
        {
            "key_id": key_id_for(GATEWAY_KEY.verify_key).value,
            "role": "gateway",
            "subject": GATEWAY_SUBJECT,
        },
        {
            "key_id": key_id_for(LOCAL_TEST_KEY.verify_key).value,
            "role": "provider",
            "subject": LOCAL_QWEN_PROVIDER_ID.value,
        },
    ]


Run = Callable[[Path], dict[str, Any]]


def _run(
    db_dir: Path,
    *,
    run_id: str,
    title: str,
    summary: str,
    harness: str,
    facility: Facility,
    procurement: ProcurementClass,
    preset: str | None,
    world: MarketWorld | None = None,
    providers: Callable[[MarketWorld, Clock], tuple[Any, ...]] | None = None,
    crash_point: str | None = None,
    before_reopen: Callable[[Path, Recorder], None] | None = None,
    retrieve_and_acknowledge: bool = False,
) -> dict[str, Any]:
    world = world or market_world()
    clock = Clock()
    key = f"product-p0-{run_id}"
    rec = Recorder(facility, key, clock)
    rec.crash_point = crash_point
    db = db_dir / f"{run_id}.sqlite3"
    chosen = None if providers is None else providers(world, clock)
    service = open_service(
        db, world, providers=chosen, clock=clock, register=(facility,), crash_hook=rec.hook
    )
    rec.service = service
    the_job = replace(job(1, facility, procurement), idempotency_key=key)
    try:
        report = service.purchase(the_job, agent_signer)
        assert not isinstance(report, PurchaseRefused), report
        rec.snapshot("purchase_returned", "IntegrationService.purchase returned")
    except SimulatedCrash:
        rec.snapshot("process_interrupted", "simulated process death (I1 test harness)")
        service.close()
        if before_reopen is not None:
            before_reopen(db, rec)
        service = open_service(db, world, clock=clock, recover=False)
        rec.service = service
        rec.snapshot("reopened", "new process; journal before recovery")
        reconciled = service.recover()
        rec.snapshot(
            "recovered",
            "IntegrationService.recover reconciled the journal with Core",
            reconciliations=[
                {"before": r.before.value, "after": r.after.value} for r in reconciled
            ],
        )
    if retrieve_and_acknowledge:
        response = retrieve(service, rec.operation_id)
        rec.snapshot(
            "retrieved",
            "agent-signed retrieve authorization accepted (TEST agent key, outside the UI)",
            response_utf8=response.decode("utf-8"),
        )
        acknowledge(service, rec.operation_id)
        rec.snapshot(
            "acknowledged",
            "agent-signed acknowledge authorization accepted; response bytes deleted",
        )
    record = {
        "run_id": run_id,
        "title": title,
        "summary": summary,
        "harness": harness,
        "procurement_preset": preset,
        "facility": _c(facility),
        "procurement_class": _c(procurement),
        "operation": operation_record(service, rec.operation_id),
        "steps": rec.steps,
        "core_records": core_records(service, facility.facility_id),
    }
    service.close()
    return record


NO_HARNESS = "none: a normal IntegrationService run"


def runs(db_dir: Path) -> list[dict[str, Any]]:
    nebius_only = replace(
        CONTROLLED_FACILITY,
        facility_id=FacilityId("facility-market-controlled-nebius-only"),
        allowed_providers=(NEBIUS_PROVIDER_ID,),
    )

    def move_chain_head(db: Path, rec: Recorder) -> None:
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO facility_chain_heads (facility_id, receipt_digest) VALUES (?, ?)",
                (rec.facility_id.value, "sha256:" + "ee" * 32),
            )

    return [
        _run(
            db_dir,
            run_id="scenario-a-settled",
            title="Scenario A: general facility, accounting settled, retrieved, acknowledged",
            summary="Both providers qualify; the cheaper Nebius offer is chosen; its Tier 1 "
            "delivery settles in Core accounting; the agent retrieves and acknowledges.",
            harness=NO_HARNESS,
            facility=GENERAL_FACILITY,
            procurement=GENERAL,
            preset="scenario-a-general",
            retrieve_and_acknowledge=True,
        ),
        _run(
            db_dir,
            run_id="scenario-b-settled",
            title="Scenario B: controlled facility (Q8_0, Tier 2), accounting settled",
            summary="Core rejects Nebius (PROFILE_NOT_ALLOWED); the local Qwen offer is chosen; "
            "its Tier 2 delivery settles in Core accounting; the agent retrieves and acknowledges.",
            harness=NO_HARNESS,
            facility=CONTROLLED_FACILITY,
            procurement=CONTROLLED,
            preset="scenario-b-controlled",
            retrieve_and_acknowledge=True,
        ),
        _run(
            db_dir,
            run_id="rejected-no-qualifying-offer",
            title="Rejected: no qualifying offer",
            summary="The facility allows only Nebius but its procurement class requires Q8_0 at "
            "Tier 2. Core rejects both offers; nothing is reserved and no provider is called.",
            harness=NO_HARNESS,
            facility=nebius_only,
            procurement=CONTROLLED,
            preset=None,
        ),
        _run(
            db_dir,
            run_id="released-provider-not-connected",
            title="Released: chosen provider not connected",
            summary="Core reserves for Nebius, but this process has no Nebius connection. No "
            "delivery attempt exists, so Core releases the hold with an audit record.",
            harness=NO_HARNESS,
            facility=GENERAL_FACILITY,
            procurement=GENERAL,
            preset=None,
            providers=lambda world, clock: (world.local,),
        ),
        _run(
            db_dir,
            run_id="unknown-provider-failure",
            title="Unknown: provider failed after the attempt began",
            summary="The attempt was recorded and claimed, then the Nebius adapter reported a "
            "provider execution error. Core cannot rule out a chargeable execution: unknown, "
            "never retried, the hold stays until expiry.",
            harness="fake Nebius transport returns a reply without choices or usage",
            facility=GENERAL_FACILITY,
            procurement=GENERAL,
            preset=None,
            world=market_world(reply={"id": "chatcmpl-synthetic", "object": "chat.completion"}),
        ),
        _run(
            db_dir,
            run_id="interrupted-after-attempt",
            title="Interrupted after the delivery attempt: unknown",
            summary="The process dies after Core recorded the attempt. Recovery marks the attempt "
            "unknown (interrupted); nothing is charged or released.",
            harness="simulated process death at after_attempt_begun (I1 test harness)",
            facility=GENERAL_FACILITY,
            procurement=GENERAL,
            preset=None,
            crash_point="after_attempt_begun",
        ),
        _run(
            db_dir,
            run_id="interrupted-before-attempt",
            title="Interrupted before any attempt: released with audit",
            summary="The process dies after Core reserved and before any delivery attempt. "
            "Recovery releases the hold (abandoned_before_execution).",
            harness="simulated process death at after_reserve (I1 test harness)",
            facility=GENERAL_FACILITY,
            procurement=GENERAL,
            preset=None,
            crash_point="after_reserve",
        ),
        _run(
            db_dir,
            run_id="disputed-chain-broken",
            title="Disputed: CHAIN_BROKEN at settlement",
            summary="Evidence was captured, then Core's chain head moved. Settlement rejects "
            "CHAIN_BROKEN; the hold, the dispute and the exact evidence are kept.",
            harness="simulated process death at after_evidence_stored, then Core's chain head "
            "moved out of band in SQLite (I1 test harness manipulation; cannot arise in the "
            "integrated path)",
            facility=GENERAL_FACILITY,
            procurement=GENERAL,
            preset=None,
            crash_point="after_evidence_stored",
            before_reopen=move_chain_head,
        ),
        _run(
            db_dir,
            run_id="late-delivery",
            title="Late: delivery outlived the reservation",
            summary="The provider returned after the 600-second hold expired. The evidence is "
            "kept as a late-delivery dispute and never settled.",
            harness=f"the test clock jumps {TTL_SECONDS + 30} s while the provider runs",
            facility=GENERAL_FACILITY,
            procurement=GENERAL,
            preset=None,
            providers=lambda world, clock: (
                ClockJump(world.nebius, clock, TTL_SECONDS + 30),
                world.local,
            ),
        ),
    ]


def render() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        recorded = runs(Path(tmp))
    record = {
        "id": "api-p0-offline-operations",
        "kind": "controlled-offline-transcripts",
        "regenerable": True,
        "baseline": "a3ec0a31f262e325061761e72c3b11246e2a400d",
        "provenance": (
            "Real IntegrationService runs over the I1 offline test world: real NebiusProvider "
            "adapter over a FAKE transport (synthetic reply), FAKE signing provider standing in "
            "for local Qwen, TEST keys (NOT FOR PRODUCTION), deterministic clock, temporary "
            "SQLite. No network, model, Nebius call or XRPL."
        ),
        "quote_provenance": (
            "configured DEMO prices in micro-tRLUSD (tRLUSD is a test token); not Nebius's "
            "billed price"
        ),
        "accounting_note": (
            "Accounting settlement is not XRPL settlement. Drawn is Credence's record of "
            "verified spending, not confirmed loan principal. No XRPL transaction exists."
        ),
        "interruption_points": list(CRASH_POINTS),
        "registry": registry_record(),
        "runs": recorded,
    }
    return json.dumps(record, indent=2, ensure_ascii=True) + "\n"


def main() -> None:
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(render(), encoding="ascii")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
