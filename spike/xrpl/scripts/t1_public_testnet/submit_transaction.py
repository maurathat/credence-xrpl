"""Submit the exact signed T1 blob once, with durable replay refusal."""

from __future__ import annotations

import argparse
from pathlib import Path

from .workflow import (
    OperationJournal,
    default_journal_path,
    load_authorization,
    load_signed,
    require_explicit_execute,
    submit_once,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    require_explicit_execute(args.execute)
    authorization = load_authorization(args.authorization, operation="submit")
    journal = OperationJournal(default_journal_path())
    try:
        result = submit_once(authorization, load_signed(args.input), journal)
    finally:
        journal.close()
    print(f"T1 provisional submission result: {result['engine_result']}; poll for finality")


if __name__ == "__main__":
    main()
