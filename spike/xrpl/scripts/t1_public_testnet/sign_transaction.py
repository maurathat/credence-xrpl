"""Sign one frozen T1 artifact locally after separate execution authorization."""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path

from .workflow import (
    issue_execution_capability,
    load_authorization,
    load_unsigned,
    require_explicit_execute,
    require_runtime_private_path,
    sign_unsigned,
    write_new_json,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    require_explicit_execute(args.execute)
    authorization = load_authorization(args.authorization, operation="sign")
    capability = issue_execution_capability(
        authorization,
        operation="sign",
        execution_intent=args.execute,
    )
    output = require_runtime_private_path(args.output)
    credential = getpass.getpass("Testnet-only source signing credential: ")
    try:
        artifact = sign_unsigned(
            authorization,
            load_unsigned(args.input),
            credential=credential,
            capability=capability,
            execution_intent=args.execute,
        )
    finally:
        credential = ""
    write_new_json(output, artifact)
    print(f"Signed T1 artifact created at {output}; transaction hash recorded internally")


if __name__ == "__main__":
    main()
