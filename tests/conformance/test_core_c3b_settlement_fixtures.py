"""Credence Core C3B settlement fixtures (vectors/c3b/): byte-identical regeneration by re-running
every scenario through the real coordinator, plus the accounting facts each fixture must show."""

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[2] / "vectors" / "c3b"
MANIFEST = {name: sha for sha, name in json.loads((FIXTURES / "MANIFEST.json").read_text())}


def load(name: str) -> dict[str, Any]:
    fixture: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="ascii"))
    return fixture


def test_manifest_matches_files() -> None:
    on_disk = {p.name for p in FIXTURES.glob("*.json")} - {"MANIFEST.json"}
    assert (
        on_disk
        == set(MANIFEST)
        == {
            "successful-settlement.json",
            "rejected-settlement.json",
            "duplicate-settlement.json",
            "expiration.json",
            "rollback.json",
        }
    )
    for name, sha in MANIFEST.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == sha


def test_regeneration_is_byte_identical() -> None:
    path = FIXTURES.parent / "build_c3b_settlement_fixtures.py"
    spec = importlib.util.spec_from_file_location("build_c3b_settlement_fixtures", path)
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    first, second = generator.render(), generator.render()
    assert first == second
    assert first == {p.name: p.read_text(encoding="ascii") for p in FIXTURES.glob("*.json")}


def test_successful_settlement_accounting() -> None:
    final = load("successful-settlement")["final"]
    assert (final["drawn_micro_trlusd"], final["held_micro_trlusd"]) == (60_000, 0)
    assert final["headroom_micro_trlusd"] == 940_000
    assert final["chain_head"] is not None and final["disputes"] == []


def test_rejected_settlement_release_rules() -> None:
    fixture = load("rejected-settlement")
    outcomes = [s.get("outcome") for s in fixture["steps"] if s["action"] == "settle"]
    assert outcomes == ["rejected_submission", "delivery_failed"]
    statuses = [r["status"] for r in fixture["final"]["reservations"].values()]
    assert statuses == ["held", "released"]
    assert fixture["final"]["drawn_micro_trlusd"] == 0


def test_duplicate_settlement_draws_once() -> None:
    fixture = load("duplicate-settlement")
    assert fixture["final"]["drawn_micro_trlusd"] == 60_000
    settles = [s for s in fixture["steps"] if s["action"] == "settle"]
    assert settles[1]["refused"] == "NOT_HELD"
    assert settles[2]["outcome"] == "rejected_submission"


def test_expiration_refuses_and_frees_headroom() -> None:
    fixture = load("expiration")
    settles = [s for s in fixture["steps"] if s["action"] == "settle"]
    assert [s["refused"] for s in settles] == ["EXPIRED", "NOT_HELD"]
    assert fixture["final"]["held_micro_trlusd"] == 0
    assert fixture["final"]["drawn_micro_trlusd"] == 0


def test_rollback_leaves_no_partial_state() -> None:
    for case in load("rollback")["cases"]:
        after = case["after_fault"]
        assert after["drawn_micro_trlusd"] == 0, case["fault_point"]
        assert after["chain_head"] is None
        assert [r["status"] for r in after["reservations"].values()] == ["held"]
        assert case["final"]["drawn_micro_trlusd"] == 60_000
