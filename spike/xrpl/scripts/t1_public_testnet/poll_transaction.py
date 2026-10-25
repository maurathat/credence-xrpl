"""Poll the submitted T1 hash until validated success or a final fail-closed stop."""

from __future__ import annotations

import argparse
from pathlib import Path

from .workflow import (
    OperationJournal,
    default_journal_path,
    load_authorization,
    load_signed,
    poll_validated,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    authorization = load_authorization(args.authorization, operation="poll")
    journal = OperationJournal(default_journal_path())
    try:
        result = poll_validated(authorization, load_signed(args.input), journal)
    finally:
        journal.close()
    print(
        "T1 validated:",
        f"hash={result['transaction_hash']}",
        f"ledger_index={result['ledger_index']}",
    )


if __name__ == "__main__":
    main()
