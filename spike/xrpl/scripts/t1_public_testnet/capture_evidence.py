"""Capture sanitized T1 evidence agreed by two public native Testnet endpoints."""

from __future__ import annotations

import argparse
from pathlib import Path

from .workflow import (
    OperationJournal,
    ValidationError,
    capture_independent_evidence,
    default_journal_path,
    load_authorization,
    load_signed,
    write_new_json,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    authorization = load_authorization(args.authorization, operation="capture")
    artifact = load_signed(args.input)
    transaction_hash = artifact.get("transaction_hash")
    if not isinstance(transaction_hash, str):
        raise ValidationError("signed artifact omitted transaction hash")
    journal = OperationJournal(default_journal_path())
    try:
        try:
            evidence = capture_independent_evidence(authorization, artifact)
            write_new_json(args.output, evidence)
        except Exception:
            journal.update(
                authorization.authorization_id,
                "submit",
                transaction_hash,
                "evidence_capture_failed",
            )
            raise
        journal.update(
            authorization.authorization_id,
            "submit",
            transaction_hash,
            "evidence_captured",
            {"evidence_path": str(args.output)},
        )
    finally:
        journal.close()
    print(f"Validated T1 evidence created at {args.output}")


if __name__ == "__main__":
    main()
