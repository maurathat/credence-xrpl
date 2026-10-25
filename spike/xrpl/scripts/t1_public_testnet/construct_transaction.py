"""Construct the frozen unsigned T1 Payment from validated Testnet state."""

from __future__ import annotations

import argparse
from pathlib import Path

from .workflow import construct_unsigned, load_authorization, write_new_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    authorization = load_authorization(args.authorization, operation="construct")
    write_new_json(args.output, construct_unsigned(authorization))
    print(f"Unsigned T1 artifact created at {args.output}")


if __name__ == "__main__":
    main()
