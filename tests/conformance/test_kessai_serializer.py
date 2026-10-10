"""Kessai serializer (JCS (RFC 8785) + NFC), imported from the pinned custody commit.

Checkpoint 0 interim (option b): custody is a git submodule at third_party/custody, pinned
to 5dc15ff6fb097dc5f4a47e72969fae4a296620a5, and canon.py is imported from its impl/ directory.
Nothing is copied or reimplemented here. canon.py only computes dataset_id for the fixed
dataset.v1 shape; a general serializer package is an open blocker for Credence Core.

Expected digests come from custody's own example/chain.json, not from this test.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

CUSTODY = Path(__file__).resolve().parents[2] / "third_party" / "custody"
PINNED_COMMIT = "5dc15ff6fb097dc5f4a47e72969fae4a296620a5"

if not (CUSTODY / "impl" / "canon.py").is_file():
    pytest.fail("third_party/custody is missing: run `git submodule update --init`", pytrace=False)
sys.path.insert(0, str(CUSTODY / "impl"))

import canon  # noqa: E402


def _chain() -> dict[str, list[dict[str, str]]]:
    return json.loads((CUSTODY / "example" / "chain.json").read_text(encoding="utf-8"))


def _payload(label: str) -> dict[str, str]:
    return next(p for p in _chain()["payloads"] if p["label"] == label)


def test_submodule_is_at_pinned_commit() -> None:
    head = (CUSTODY / ".git").read_text().strip() if (CUSTODY / ".git").is_file() else ""
    if head.startswith("gitdir:"):
        gitdir = (CUSTODY / head.removeprefix("gitdir:").strip()).resolve()
        assert (gitdir / "HEAD").read_text().strip() == PINNED_COMMIT
    else:
        pytest.skip("custody is not a submodule checkout; cannot read its HEAD")


def test_canon_matches_custody_provenance_hash() -> None:
    provenance = (CUSTODY / "impl" / "CANON_PROVENANCE").read_text(encoding="utf-8")
    expected = next(
        line.split()[1] for line in provenance.splitlines() if line.startswith("sha256")
    )
    actual = hashlib.sha256((CUSTODY / "impl" / "canon.py").read_bytes()).hexdigest()
    assert actual == expected


def test_dataset_v1_digest_matches_custody_vector() -> None:
    wire = _payload("wire-0")
    assert canon.dataset_id(wire["text"].encode("utf-8")) == wire["dataset_id"]


def test_dataset_v1_round_trip_through_reencoding() -> None:
    """The same dataset, re-encoded (reordered, pretty-printed, 1 -> 1.0), keeps its digest."""
    original, reencoded = _payload("wire-0"), _payload("wire-1")
    assert original["text"] != reencoded["text"]
    assert reencoded["dataset_id"] == original["dataset_id"]
    assert canon.dataset_id(reencoded["text"].encode("utf-8")) == original["dataset_id"]
