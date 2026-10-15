"""contracts/CORE-OBJECTS.md matches the implemented C1A schema and the committed vectors.

Parses the contract's tables and lists and compares them with the dataclasses, enums and refusal
codes in credence/core, and with vectors/c1. Standard library only.
"""

import ast
import dataclasses
import inspect
import json
import re
from enum import Enum
from pathlib import Path

import pytest

from credence.core import kessai, objects, primitives

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / "contracts" / "CORE-OBJECTS.md").read_text(encoding="utf-8")
NESTED = {"sampling": objects.SamplingPolicy}
CLASSES: dict[str, type] = {cls.OBJECT_TYPE: cls for cls in objects.OBJECT_TYPES} | NESTED


def _sections() -> dict[str, str]:
    found: dict[str, str] = {}
    heading, lines = "", []
    for line in TEXT.splitlines():
        if line.startswith("#"):
            found[heading] = "\n".join(lines)
            heading, lines = line.lstrip("#").strip(), []
        else:
            lines.append(line)
    found[heading] = "\n".join(lines)
    return found


SECTIONS = _sections()
OBJECT_SECTIONS = {
    m.group(1): body for h, body in SECTIONS.items() if (m := re.fullmatch(r"`([a-z_.]+)`", h))
}
ROW = re.compile(r"^\| `(\w+)` \| `([^`]+)` \| ([^|]+?) \|", re.M)


def _rows(name: str) -> list[tuple[str, str, str]]:
    return ROW.findall(OBJECT_SECTIONS[name])


def _ticked(body: str) -> list[str]:
    return re.findall(r"`([^`]+)`", body)


def test_every_object_type_is_documented_and_nothing_else() -> None:
    assert set(OBJECT_SECTIONS) == set(CLASSES)


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_object_members_types_and_nullability(name: str) -> None:
    cls = CLASSES[name]
    assert f"Class `{cls.__name__}`" in OBJECT_SECTIONS[name]
    rows = _rows(name)
    assert tuple(member for member, _, _ in rows) == cls.MEMBERS  # type: ignore[attr-defined]
    annotations = {f.name: f.type for f in dataclasses.fields(cls)}
    for member, documented_type, null in rows:
        annotation = annotations[member]
        if annotation == "None":
            assert (documented_type, null.split()[0]) == ("None", "always"), member
        elif annotation.endswith(" | None"):
            assert documented_type == annotation.removesuffix(" | None"), member
            assert null.split()[0] == "yes", member
        else:
            assert (documented_type, null) == (annotation, "no"), member


def test_envelope_and_schema_version() -> None:
    match = re.search(r'`schema_version` is `"(\d+)"`', TEXT)
    assert match is not None and match.group(1) == objects.SCHEMA_VERSION
    assert "begins with `object_type` and `schema_version`" in TEXT


VOCABULARIES = {
    name: cls
    for name, cls in inspect.getmembers(primitives, inspect.isclass)
    if issubclass(cls, Enum) and cls.__module__ == primitives.__name__
}


def test_closed_vocabularies_match_exactly() -> None:
    rows = re.findall(r"^\| `(\w+)` \| (.+) \|$", SECTIONS["Closed vocabularies"], re.M)
    documented = {name: [json.loads(t) for t in _ticked(values)] for name, values in rows}
    assert set(documented) == set(VOCABULARIES)
    for name, values in documented.items():
        actual = [member.value for member in VOCABULARIES[name]]
        assert values == actual, name
        assert [type(v) for v in values] == [type(v) for v in actual], name


PRIMITIVES = {
    name
    for name, cls in inspect.getmembers(primitives, inspect.isclass)
    if dataclasses.is_dataclass(cls)
    and cls.__module__ == primitives.__name__
    and not name.startswith("_")
}


def test_primitive_types_match_exactly() -> None:
    rows = re.findall(r"^\| `(\w+)` \| (?:integer|string) \|", SECTIONS["Primitive types"], re.M)
    assert set(rows) == PRIMITIVES


def test_every_documented_type_resolves() -> None:
    known = PRIMITIVES | set(VOCABULARIES) | {"int", "str", "None", "SamplingPolicy"}
    for name in CLASSES:
        for member, documented_type, _ in _rows(name):
            inner = re.fullmatch(r"tuple\[(\w+), \.\.\.\]", documented_type)
            assert (inner.group(1) if inner else documented_type) in known, member


# --- refusal codes, read from the source with ast ------------------------------------------


def _calls(path: Path, callee: str, position: int, functions: set[str] | None = None) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scopes: list[ast.AST] = (
        [tree]
        if functions is None
        else [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in functions]
    )
    codes = set()
    for scope in scopes:
        for node in ast.walk(scope):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == callee
                and len(node.args) > position
                and isinstance(node.args[position], ast.Constant)
                and isinstance(node.args[position].value, str)
            ):
                codes.add(node.args[position].value)
    return codes


def _documented(section: str) -> set[str]:
    return set(_ticked(SECTIONS[section]))


def test_domain_refusal_codes_match_source() -> None:
    core = ROOT / "credence" / "core"
    source = set()
    for module in ("primitives.py", "objects.py"):
        source |= _calls(core / module, "DomainError", 0)
        source |= _calls(core / module, "_pattern_string", 3)
    assert _documented("Domain refusal codes") == source


def test_adapter_codes_match_source() -> None:
    source = _calls(ROOT / "credence" / "core" / "kessai.py", "SerializationRejected", 0)
    assert _documented("Serializer adapter codes") == source


def test_canon_pass_through_codes_match_the_pipeline_steps_called() -> None:
    steps = {
        "_decode",
        "_reject_duplicates",
        "_integral_number",
        "_non_finite",
        "_require_scalar_unicode",
        "_admit",
        "_fold_nfc",
    }
    source = _calls(kessai.CANON_PATH, "Reject", 0, functions=steps)
    assert _documented("canon.py codes passed through unchanged") == source


# --- vectors -------------------------------------------------------------------------------


def test_every_positive_object_vector_matches_its_documented_members() -> None:
    for path in sorted((ROOT / "vectors" / "c1").glob("*.json")):
        if path.name == "MANIFEST.json":
            continue
        vector = json.loads(path.read_text(encoding="ascii"))
        if not vector["entry"].startswith("objects.") or "reject" in vector["expected"]:
            continue
        value = vector["input"]
        members = tuple(m for m, _, _ in _rows(value["object_type"]))
        assert set(value) - {"object_type", "schema_version"} == set(members), path.name
        assert value["schema_version"] == objects.SCHEMA_VERSION
        if "sampling" in value:
            assert set(value["sampling"]) == {m for m, _, _ in _rows("sampling")}
