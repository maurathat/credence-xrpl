"""Generate version-2 journal fixtures from commit 607cc2a's code (X1a, journal schema 2).

This script must be run with an archive of that commit as the working directory and first
PYTHONPATH entry. See this directory's README for the exact command.
"""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import credence
from credence.xrpl_adapter.journal import PaymentJournal
from credence.xrpl_adapter.types import (
    PreparedPayment,
    SubmissionDisposition,
    TransactionState,
)

assert Path(credence.__file__).resolve().is_relative_to(Path.cwd().resolve()), credence.__file__

helpers = runpy.run_path("tests/unit/test_xrpl_payment_readiness.py")
payment_request = helpers["payment_request"]
signed_blob = helpers["signed_blob"]

journal = PaymentJournal(Path(sys.argv[1]))
if len(sys.argv) == 3 and sys.argv[2] == "--empty":
    raise SystemExit(0)

OPERATIONS = ("v2-success", "v2-failure", "v2-short-delivery", "v2-expired", "v2-pending")
for offset, operation_id in enumerate(OPERATIONS):
    request = payment_request(operation_id, sequence=200 + offset)
    journal.persist_signed(PreparedPayment(request), signed_blob(request))

for operation_id in OPERATIONS:
    attempt = journal.begin_submission(operation_id, resubmission=False)
    assert attempt is not None
    journal.finish_submission(
        operation_id,
        attempt,
        disposition=SubmissionDisposition.ACKNOWLEDGED,
        next_state=TransactionState.PENDING_CONFIRMATION,
        provisional_status="tesSUCCESS",
    )


def evidence(operation_id: str, result: str, delivered: str | None) -> bytes:
    return json.dumps(
        {
            "fixture": operation_id,
            "meta": {"TransactionResult": result, "delivered_amount": delivered},
        },
        sort_keys=True,
    ).encode()


def finalize(
    operation_id: str, state: TransactionState, result: str, delivered: int | None
) -> None:
    journal.finalize(
        operation_id,
        state=state,
        result=result,
        detail=f"{operation_id} fixture outcome",
        evidence=evidence(operation_id, "tesSUCCESS" if delivered else result, str(delivered)),
        validated_ledger_index=23,
        validated_ledger_hash=f"{23:064X}",
        verified_delivered_amount_micro_units=delivered,
        evidence_verification_version=1,
    )


finalize("v2-success", TransactionState.VALIDATED_SUCCESS, "tesSUCCESS", 1_500_000)
assert journal.claim_settlement_effect("v2-success")  # permitted at 607cc2a; retired in X1c
finalize("v2-failure", TransactionState.VALIDATED_FAILURE, "tecPATH_DRY", None)
# The X1a representation of a short delivery that C1 replaces.
finalize(
    "v2-short-delivery", TransactionState.VALIDATED_FAILURE, "DELIVERED_AMOUNT_MISMATCH", 100_000
)
journal.finalize(
    "v2-expired",
    state=TransactionState.EXPIRED_NOT_INCLUDED,
    result="txnNotFound",
    detail="v2-expired fixture outcome",
    evidence=b'{"fixture":"v2-expired"}',
    validated_ledger_index=26,
    validated_ledger_hash=f"{26:064X}",
    verified_delivered_amount_micro_units=None,
    evidence_verification_version=1,
)
