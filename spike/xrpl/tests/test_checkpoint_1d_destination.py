"""Regression tests for Checkpoint 1D-P evidence destination plumbing."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from harness import write_json  # noqa: E402
from run_checkpoint_1d import (  # noqa: E402
    CheckpointStop,
    Runner,
    evidence_reference,
    finalize_evidence,
    prepare_evidence_destination,
    resolve_evidence_destination,
)

ASSERTIONS_SHA256 = "656921cacd310214b6ecce7088e21e84fcca44feaa3104b95cfb9f5f4bc92b92"
ORACLE_SHA256 = "8052459639081eca2baeb64402120ce00507f35eafcc595315f912016ae07bbf"
E5_TEST_SHA256 = "4a8eba0b49f8a30c50cc5eddfed7a7a8bdc51a29c5c4b241780738d6e0acc7d0"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(item.read_bytes())
    return digest.hexdigest()


def temporary_roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo_root = tmp_path / "repo"
    xrpl_root = repo_root / "spike" / "xrpl"
    evidence_root = xrpl_root / "evidence"
    evidence_root.mkdir(parents=True)
    return repo_root, xrpl_root, evidence_root


def test_p1_explicit_checkpoint_1d_x_destination_is_accepted(tmp_path: Path) -> None:
    repo_root, _xrpl_root, evidence_root = temporary_roots(tmp_path)
    destination = prepare_evidence_destination(
        "spike/xrpl/evidence/checkpoint-1d-x",
        evidence_root=evidence_root,
        repo_root=repo_root,
    )
    assert destination == evidence_root / "checkpoint-1d-x"


def test_p2_writes_use_selected_destination_not_historical_path(tmp_path: Path) -> None:
    repo_root, _xrpl_root, evidence_root = temporary_roots(tmp_path)
    destination = prepare_evidence_destination(
        "checkpoint-1d-x",
        evidence_root=evidence_root,
        repo_root=repo_root,
    )
    runner = Runner(
        client=object(),  # type: ignore[arg-type]
        evidence_dir=destination,
        transactions_dir=destination / "transactions",
    )
    runner.write_evidence("provenance.json", {"destination": "checkpoint-1d-x"})
    assert (destination / "provenance.json").is_file()
    assert not (evidence_root / "checkpoint-1d" / "provenance.json").exists()


def test_p3_historical_checkpoint_1d_evidence_remains_identical(tmp_path: Path) -> None:
    historical = ROOT / "evidence" / "checkpoint-1d"
    before = tree_digest(historical)
    repo_root, _xrpl_root, evidence_root = temporary_roots(tmp_path)
    prepare_evidence_destination(
        "checkpoint-1d-x",
        evidence_root=evidence_root,
        repo_root=repo_root,
    )
    assert tree_digest(historical) == before


def test_p4_missing_explicit_destination_fails_closed(tmp_path: Path) -> None:
    repo_root, _xrpl_root, evidence_root = temporary_roots(tmp_path)
    with pytest.raises(CheckpointStop, match="explicit evidence destination"):
        resolve_evidence_destination(None, evidence_root=evidence_root, repo_root=repo_root)


def test_p5_destination_outside_evidence_root_fails(tmp_path: Path) -> None:
    repo_root, _xrpl_root, evidence_root = temporary_roots(tmp_path)
    with pytest.raises(CheckpointStop, match="must be under"):
        resolve_evidence_destination(
            tmp_path / "outside",
            evidence_root=evidence_root,
            repo_root=repo_root,
        )


def test_p6_path_traversal_fails(tmp_path: Path) -> None:
    repo_root, _xrpl_root, evidence_root = temporary_roots(tmp_path)
    with pytest.raises(CheckpointStop, match="path traversal"):
        resolve_evidence_destination(
            "spike/xrpl/evidence/../escape",
            evidence_root=evidence_root,
            repo_root=repo_root,
        )


def test_p7_nonempty_authoritative_destination_fails_closed(tmp_path: Path) -> None:
    repo_root, _xrpl_root, evidence_root = temporary_roots(tmp_path)
    destination = evidence_root / "checkpoint-1d-x"
    destination.mkdir()
    write_json(destination / "checkpoint-result.json", {"outcome": "BLOCKED"})
    with pytest.raises(CheckpointStop, match="not empty; refusing reuse"):
        prepare_evidence_destination(
            destination,
            evidence_root=evidence_root,
            repo_root=repo_root,
        )


def test_p8_manifest_is_generated_in_selected_destination(tmp_path: Path) -> None:
    repo_root, _xrpl_root, evidence_root = temporary_roots(tmp_path)
    destination = prepare_evidence_destination(
        "checkpoint-1d-x",
        evidence_root=evidence_root,
        repo_root=repo_root,
    )
    write_json(destination / "checkpoint-result.json", {"outcome": "PASS"})
    finalize_evidence(destination)
    manifest = json.loads((destination / "MANIFEST.json").read_text())
    assert manifest["file_count"] == 1
    assert manifest["files"][0]["path"] == "checkpoint-result.json"
    assert not (evidence_root / "checkpoint-1d" / "MANIFEST.json").exists()


def test_p9_metadata_references_selected_destination(tmp_path: Path) -> None:
    repo_root, xrpl_root, evidence_root = temporary_roots(tmp_path)
    destination = prepare_evidence_destination(
        "checkpoint-1d-x",
        evidence_root=evidence_root,
        repo_root=repo_root,
    )
    assert evidence_reference(destination, xrpl_root=xrpl_root) == "evidence/checkpoint-1d-x"


def test_p10_s1_s12_and_semantic_components_remain_unchanged() -> None:
    assert sha256(ROOT / "E5-ASSERTIONS.md") == ASSERTIONS_SHA256
    assert sha256(SCRIPTS / "e5_oracle.py") == ORACLE_SHA256
    assert sha256(ROOT / "tests" / "test_checkpoint_1d.py") == E5_TEST_SHA256
