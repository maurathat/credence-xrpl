"""contracts/SIGNATURES.md matches the implemented signature layer. Standard library only."""

import re
from pathlib import Path

from credence.core import signatures
from credence.core.signatures import ROLE_OBJECT_TYPES, KeyRole, KeyStatus, SignatureFailure

TEXT = (Path(__file__).resolve().parents[2] / "contracts" / "SIGNATURES.md").read_text()


def section(title: str) -> str:
    match = re.search(rf"^#+ {re.escape(title)}\n(.*?)(?=^#+ |\Z)", TEXT, re.M | re.S)
    assert match is not None, title
    return match.group(1)


def test_domain_and_encoding_are_stated() -> None:
    message = section("Message")
    assert f"`domain` is `{signatures.SIGNATURE_DOMAIN}`" in message
    assert "uint32 big-endian byte length" in message
    assert signatures._LENGTH_BYTES == 4


def test_roles_match() -> None:
    rows = re.findall(r"^\| `(\w+)` \| `([\w.]+)` \|", section("Roles"), re.M)
    assert {KeyRole(role): object_type for role, object_type in rows} == ROLE_OBJECT_TYPES


def test_statuses_match() -> None:
    match = re.search(r"`status` is one of (.+?)\.", section("Key registry"))
    assert match is not None
    assert re.findall(r"`(\w+)`", match.group(1)) == [s.value for s in KeyStatus]


def test_failure_order_matches() -> None:
    documented = re.findall(r"^\d+\. `(\w+)`", section("Verification"), re.M)
    assert documented == [f.value for f in SignatureFailure]


def test_fact_table_names_every_c2_signature_fact() -> None:
    facts = re.findall(r"^\| `(\w+)` \|", section("Facts for the C2 engines"), re.M)
    assert facts == [
        "request_signature_valid",
        "working_key_current",
        "gateway_signature_valid",
        "provider_signature",
    ]
    assert set(facts[:2]) == set(signatures.PolicySignatureFacts.__dataclass_fields__)
    verifier_fields = set(signatures.VerifierSignatureFacts.__dataclass_fields__)
    assert set(facts[2:]) == verifier_fields
