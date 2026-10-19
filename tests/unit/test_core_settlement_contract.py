"""contracts/SETTLEMENT-COORDINATOR.md matches the implemented coordinator. Standard library only."""

import ast
import re
from pathlib import Path

from credence.core import settlement_coordinator as sc

TEXT = (Path(__file__).resolve().parents[2] / "contracts" / "SETTLEMENT-COORDINATOR.md").read_text()
SOURCE = Path(sc.__file__).read_text()


def section(title: str) -> str:
    match = re.search(rf"^#+ {re.escape(title)}\n(.*?)(?=^#+ |\Z)", TEXT, re.M | re.S)
    assert match is not None, title
    return match.group(1)


def test_states_match() -> None:
    documented = re.findall(r"^\d+\. `(\w+)`:", section("Scope: accounting settlement only"), re.M)
    assert documented == [s.value for s in sc.SettlementState]


def test_tables_match_schema() -> None:
    documented = re.findall(r"^\| `(\w+)` \|", section("Storage"), re.M)
    created = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sc._SCHEMA)
    assert set(documented) == set(created) | {"facility_accounts"}


def test_fault_points_match() -> None:
    match = re.search(r"leaves drawn, the reservation and the receipt untouched:\n\n(.+)\n", TEXT)
    assert match is not None
    assert tuple(re.findall(r"`(\w+)`", match.group(1))) == sc.FAULT_POINTS


def test_terminal_reasons_match() -> None:
    match = re.search(r"^Terminal reasons: (.+)$", TEXT, re.M)
    assert match is not None
    documented = set(re.findall(r"`(\w+)`", match.group(1)))
    assert documented == {r.value for r in sc.TERMINAL_REASONS}


def test_refusal_codes_match_source() -> None:
    raised = {
        node.args[0].value
        for node in ast.walk(ast.parse(SOURCE))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SettlementRefused"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    documented = set(re.findall(r"`([A-Z_]+)`", section("Refusal codes")))
    assert documented == raised


def test_coordinator_has_no_xrpl_network_or_inference_dependency() -> None:
    imported = set()
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    roots = {name.split(".")[0] for name in imported}
    assert not roots & {"xrpl", "socket", "urllib", "http", "requests", "httpx"}
    assert not {
        name for name in imported if name.startswith(("credence.inference", "credence.xrpl"))
    }
    assert "SettlementRecord(" not in SOURCE  # no canonical XRPL settlement record is created
