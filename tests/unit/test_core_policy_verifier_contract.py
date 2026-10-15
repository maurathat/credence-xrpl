"""contracts/POLICY-VERIFIER.md matches the implemented C2 decision layer. Standard library only."""

import ast
import dataclasses
import re
from pathlib import Path

from credence.core import policy_engine, procurement, reservation_ledger, verifier
from credence.core.primitives import POLICY_REASON_CODES, VERIFIER_REASON_CODES, ReasonCode

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / "contracts" / "POLICY-VERIFIER.md").read_text(encoding="utf-8")


def section(title: str) -> str:
    match = re.search(rf"^#+ {re.escape(title)}\n(.*?)(?=^#+ |\Z)", TEXT, re.M | re.S)
    assert match is not None, title
    return match.group(1)


def numbered_codes(title: str) -> list[str]:
    return re.findall(r"^\d+\. `(\w+)`", section(title), re.M)


def field_rows(title: str) -> list[tuple[str, str, str]]:
    return re.findall(r"^\| `(\w+)` \| `([^`]+)` \| (.+?) \|$", section(title), re.M)


def test_policy_precedence_matches_code() -> None:
    assert numbered_codes("Policy precedence") == [c.value for c in POLICY_REASON_CODES]


def test_verifier_precedence_matches_code() -> None:
    assert numbered_codes("Verifier precedence") == [c.value for c in VERIFIER_REASON_CODES]


def test_partition_covers_the_vocabulary() -> None:
    assert {*POLICY_REASON_CODES, *VERIFIER_REASON_CODES} == set(ReasonCode)


def test_unreachable_codes_are_exactly_those_the_attack_suite_names() -> None:
    from test_core_attacks import UNREACHABLE_UNDER_C1A

    documented = re.findall(r"^`(\w+)`:", section("Unreachable under C1A typing"), re.M)
    assert documented == [c.value for c in UNREACHABLE_UNDER_C1A]


def _check_fields(title: str, cls: type) -> None:
    rows = field_rows(title)
    fields = dataclasses.fields(cls)
    assert [name for name, _, _ in rows] == [f.name for f in fields]
    for (name, documented, meaning), f in zip(rows, fields, strict=True):
        annotation = str(f.type)
        if annotation.endswith(" | None"):
            assert documented == annotation.removesuffix(" | None"), name
            assert "nullable" in meaning, name
        else:
            assert documented == annotation, name


def test_policy_input_fields_match() -> None:
    _check_fields("Policy input", policy_engine.PolicyInput)


def test_verifier_input_fields_match() -> None:
    _check_fields("Verifier input", verifier.VerifierInput)


def test_matcher_requirements_match() -> None:
    documented = numbered_codes("Procurement matching")
    assert tuple(documented) == procurement.REQUIREMENTS


def test_provider_signature_facts_match() -> None:
    match = re.search(r"`ProviderSignatureFact` values: (.+)\.", TEXT)
    assert match is not None
    documented = re.findall(r"`(\w+)`", match.group(1))
    assert documented == [fact.value for fact in verifier.ProviderSignatureFact]


def test_engine_versions_and_ttl_are_stated() -> None:
    assert f"`{policy_engine.ENGINE_VERSION.value}`" in TEXT
    assert f"`{verifier.ENGINE_VERSION.value}`" in TEXT
    assert f"ttl_seconds={reservation_ledger.DEFAULT_TTL_SECONDS}" in TEXT


def test_ledger_refusal_codes_match_source() -> None:
    tree = ast.parse(Path(reservation_ledger.__file__).read_text())
    raised = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ReservationStateError"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    documented = set(re.findall(r"`(\w+)`", section("Ledger refusal codes")))
    assert documented - {"ReservationStateError"} == raised
