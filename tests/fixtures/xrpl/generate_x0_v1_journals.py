"""Generate X0 journal fixtures from commit aa236e1's code.

This script must be run with an archive of that commit as the working directory
and first PYTHONPATH entry. See this directory's README for the exact command.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

from credence.xrpl_adapter.journal import PaymentJournal
from credence.xrpl_adapter.types import (
    PreparedPayment,
    SubmissionDisposition,
    TransactionState,
)

helpers = runpy.run_path("tests/unit/test_xrpl_payment_readiness.py")
payment_request = helpers["payment_request"]
signed_blob = helpers["signed_blob"]

journal = PaymentJournal(Path(sys.argv[1]))
if len(sys.argv) == 3 and sys.argv[2] == "--empty":
    raise SystemExit(0)

for offset, operation_id in enumerate(
    ("legacy-success", "legacy-failure", "legacy-expired", "legacy-pending")
):
    request = payment_request(operation_id, sequence=100 + offset)
    journal.persist_signed(PreparedPayment(request), signed_blob(request))

for operation_id in ("legacy-success", "legacy-failure", "legacy-expired"):
    attempt = journal.begin_submission(operation_id, resubmission=False)
    journal.finish_submission(
        operation_id,
        attempt,
        disposition=SubmissionDisposition.ACKNOWLEDGED,
        next_state=TransactionState.PENDING_CONFIRMATION,
        provisional_status="tesSUCCESS",
    )

journal.finalize(
    "legacy-success",
    state=TransactionState.VALIDATED_SUCCESS,
    result="tesSUCCESS",
    detail="legacy validated success",
    evidence=b'{"legacy":"validated-success","validated":true}',
    validated_ledger_index=23,
)
assert journal.claim_settlement_effect("legacy-success")
journal.finalize(
    "legacy-failure",
    state=TransactionState.VALIDATED_FAILURE,
    result="tecUNFUNDED_PAYMENT",
    detail="legacy validated failure",
    evidence=b'{"legacy":"validated-failure","validated":true}',
    validated_ledger_index=23,
)
journal.finalize(
    "legacy-expired",
    state=TransactionState.EXPIRED_NOT_INCLUDED,
    result="EXPIRED_NOT_INCLUDED",
    detail="legacy complete-history decision",
    evidence=b'{"legacy":"expiry-history","validated":true}',
    validated_ledger_index=26,
)

attempt = journal.begin_submission("legacy-pending", resubmission=False)
journal.finish_submission(
    "legacy-pending",
    attempt,
    disposition=SubmissionDisposition.ACKNOWLEDGED,
    next_state=TransactionState.PENDING_CONFIRMATION,
    provisional_status="terQUEUED",
)
