"""Checkpoint 1E-NR offline evidence-serialization regressions."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from e6_negative_oracle import (  # noqa: E402
    NEGATIVE_CASES,
    normalize_unordered_strings,
)
from e6_oracle import BORROWER_MULTISIGN_PREFIX_HEX  # noqa: E402
from harness import StateAssertion, serialize_json, state_assertion  # noqa: E402
from run_checkpoint_1e_n import (  # noqa: E402
    complete_evidence_skeleton,
    full_evidence_serialization_preflight,
)

HISTORICAL_BINDINGS = ROOT / "evidence" / "checkpoint-1e-n" / "live-bindings.json"


def _legacy_json_safe(value: Any) -> Any:
    """Exact pre-repair conversion retained only to reproduce the historical STOP."""

    if isinstance(value, dict):
        return {str(key): _legacy_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_legacy_json_safe(item) for item in value]
    if isinstance(value, StateAssertion):
        return asdict(value)
    return value


def _historical_n1_assertions() -> list[StateAssertion]:
    committed = json.loads(HISTORICAL_BINDINGS.read_text())
    bindings = committed["bindings"]
    transaction = bindings["case_constructions"]["N1"]["exact_signed_transaction"]
    signer_rows = [wrapper["Signer"] for wrapper in transaction["Signers"]]
    borrower = bindings["actors"]["borrower"]["account"]
    observed_signers = {row["Account"]: row["SigningPubKey"] for row in signer_rows}
    return [
        state_assertion("borrower transaction Account", transaction["Account"], borrower),
        state_assertion("borrower top-level SigningPubKey", transaction["SigningPubKey"], ""),
        state_assertion("borrower master signature absent", transaction.get("TxnSignature"), None),
        state_assertion("borrower signer identities", observed_signers, observed_signers),
        state_assertion(
            "borrower SMT-NUL domains",
            {BORROWER_MULTISIGN_PREFIX_HEX for _row in signer_rows},
            {BORROWER_MULTISIGN_PREFIX_HEX},
        ),
    ]


def test_s1_committed_n1_fixture_reproduces_old_set_serialization_failure() -> None:
    legacy_record = {"state_assertions": _historical_n1_assertions()}
    converted = _legacy_json_safe(legacy_record)
    assert converted["state_assertions"][4]["name"] == "borrower SMT-NUL domains"
    assert isinstance(converted["state_assertions"][4]["actual"], set)
    assert isinstance(converted["state_assertions"][4]["expected"], set)
    with pytest.raises(TypeError, match="Object of type set is not JSON serializable"):
        json.dumps(converted, indent=2, sort_keys=True)


@pytest.mark.parametrize(
    ("case_id", "requirement"),
    [(f"N{number}", f"S{number + 1}") for number in range(1, 7)],
)
def test_s2_s7_each_repaired_case_record_serializes(case_id: str, requirement: str) -> None:
    skeleton = complete_evidence_skeleton()
    record = skeleton["cases"][case_id]
    decoded = json.loads(serialize_json(record))
    assert decoded["case"] == case_id, requirement
    domain = next(
        item for item in decoded["state_assertions"] if item["name"] == "borrower SMT-NUL domains"
    )
    assert domain["actual"] == [BORROWER_MULTISIGN_PREFIX_HEX]
    assert domain["expected"] == [BORROWER_MULTISIGN_PREFIX_HEX]


def test_s8_unordered_domain_insertion_order_has_byte_identical_json() -> None:
    forward = normalize_unordered_strings(["domain-b", "domain-a", "domain-b"])
    reverse = normalize_unordered_strings(["domain-b", "domain-a", "domain-b"][::-1])
    assert forward == ["domain-a", "domain-b"]
    assert serialize_json({"domains": forward}) == serialize_json({"domains": reverse})


def test_s9_arbitrary_object_fails_closed_without_stringification() -> None:
    with pytest.raises(TypeError, match=r"unsupported JSON evidence type object at \$.value"):
        serialize_json({"value": object()})


def test_s10_bytes_fail_closed_without_an_explicit_encoding_rule() -> None:
    with pytest.raises(TypeError, match=r"unsupported JSON evidence type bytes at \$.value"):
        serialize_json({"value": b"not-json"})


def test_s11_complete_six_case_preflight_uses_production_serializer() -> None:
    result = full_evidence_serialization_preflight()
    assert result["result"] == "PASS"
    assert result["cases"] == {case.case_id: "PASS" for case in NEGATIVE_CASES}
    assert result["manifest_metadata"] == "PASS"
    assert result["protected_state"] == "PASS"
    assert result["authorization_signing"] == "PASS"
    assert result["result_placeholders"] == "PASS"
    assert result["phase_timing"] == "PASS"


def test_s12_bypassed_normalization_fails_before_transaction_boundary() -> None:
    skeleton = complete_evidence_skeleton()
    skeleton["cases"]["N1"]["state_assertions"][0]["actual"] = {BORROWER_MULTISIGN_PREFIX_HEX}
    with pytest.raises(
        TypeError,
        match=(
            r"unsupported JSON evidence type set at "
            r"\$.cases.N1.state_assertions\[0\].actual"
        ),
    ):
        full_evidence_serialization_preflight(skeleton=skeleton)
