"""Fail if two decision records share a number, or a record's title disagrees with its filename.

Decision records live in docs/decisions/ as NNNN-<slug>.md with a first line "# NNNN. <title>".
"""

import re
import sys
from collections import defaultdict
from pathlib import Path

DECISIONS = Path(__file__).resolve().parents[1] / "docs" / "decisions"
NAME = re.compile(r"^(\d{4})-[a-z0-9][a-z0-9-]*\.md$")
TITLE = re.compile(r"^# (\d{4})\. \S")


def problems(directory: Path = DECISIONS) -> list[str]:
    found: list[str] = []
    by_number: dict[str, list[str]] = defaultdict(list)
    for path in sorted(directory.glob("*.md")):
        name = NAME.match(path.name)
        if name is None:
            found.append(f"{path.name}: not named NNNN-<slug>.md")
            continue
        number = name.group(1)
        by_number[number].append(path.name)
        lines = path.read_text(encoding="utf-8").splitlines()
        title = TITLE.match(lines[0]) if lines else None
        if title is None or title.group(1) != number:
            found.append(f"{path.name}: first line must be '# {number}. <title>'")
    for number, names in sorted(by_number.items()):
        if len(names) > 1:
            found.append(f"decision {number} is used by {', '.join(names)}")
    return found


def main() -> int:
    found = problems()
    for problem in found:
        print(problem, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
