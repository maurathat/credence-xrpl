"""Fail if any tracked file exceeds the size limit (evidence dumps belong outside git)."""

import subprocess
import sys
from pathlib import Path

LIMIT_BYTES = 5 * 1024 * 1024


def main() -> int:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], check=True, capture_output=True, text=True
    ).stdout.split("\0")
    too_big = [
        (p, Path(p).stat().st_size)
        for p in tracked
        if p and Path(p).is_file() and Path(p).stat().st_size > LIMIT_BYTES
    ]
    for path, size in too_big:
        print(f"{path}: {size} bytes > {LIMIT_BYTES} limit", file=sys.stderr)
    return 1 if too_big else 0


if __name__ == "__main__":
    sys.exit(main())
