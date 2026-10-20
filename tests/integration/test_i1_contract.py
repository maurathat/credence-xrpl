"""contracts/INTEGRATION-FLOW.md matches credence/integration/. Standard library only."""

import ast
import re
from pathlib import Path

from credence.integration import journal, service

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / "contracts" / "INTEGRATION-FLOW.md").read_text()


def section(title: str) -> str:
    match = re.search(rf"^#+ {re.escape(title)}\n(.*?)(?=^#+ |\Z)", TEXT, re.M | re.S)
    assert match is not None, title
    return match.group(1)


def listed(title: str, label: str) -> list[str]:
    match = re.search(rf"{re.escape(label)}(.+?)\.\n", section(title))
    assert match is not None, label
    return re.findall(r"`(\w+)`", match.group(1))


def test_states_match() -> None:
    assert listed("States", "values, in lifecycle order: ") == [
        s.value for s in journal.OperationState
    ]
    assert set(listed("States", "In flight (the facility is busy): ")) == {
        s.value for s in journal.IN_FLIGHT
    }
    assert set(listed("States", "Response available to the authorized agent: ")) == {
        s.value for s in journal.RESPONSE_AVAILABLE
    }
    rows = re.findall(r"^\| `(\w+)` \|", section("States"), re.M)
    assert rows == [s.value for s in journal.OperationState]


def test_crash_points_match() -> None:
    match = re.search(r"Interruption points are tested at each of:\n\n(.+)\n", TEXT)
    assert match is not None
    assert tuple(re.findall(r"`(\w+)`", match.group(1))) == service.CRASH_POINTS


def test_refusals_match() -> None:
    assert listed("Refusals", "`Refusal` values: ") == [r.value for r in service.Refusal]


def test_integration_has_no_network_or_xrpl_dependency() -> None:
    for module in (journal, service):
        source = Path(module.__file__).read_text()  # type: ignore[arg-type]
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        roots = {name.split(".")[0] for name in imported}
        assert not roots & {"xrpl", "socket", "urllib", "http", "requests", "httpx"}
        assert not {name for name in imported if name.startswith("credence.xrpl")}
        assert "SettlementRecord" not in source
