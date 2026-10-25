"""Fund two pre-existing, Testnet-only classic addresses through the official faucet."""

from __future__ import annotations

import argparse
from pathlib import Path

from .workflow import (
    OperationJournal,
    default_journal_path,
    load_authorization,
    require_explicit_execute,
    setup_accounts,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    require_explicit_execute(args.execute)
    authorization = load_authorization(args.authorization, operation="faucet")
    journal = OperationJournal(default_journal_path())
    try:
        statuses = setup_accounts(authorization, journal)
    finally:
        journal.close()
    print("T1 account setup:", ", ".join(f"{role}={status}" for role, status in statuses.items()))


if __name__ == "__main__":
    main()
