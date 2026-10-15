"""Credence Core C3A signature vectors (vectors/c3a/) against the live code and against PyNaCl
directly. The message is also rebuilt here by hand from its documented encoding."""

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import nacl.exceptions
import pytest
from nacl.signing import VerifyKey

from credence.core import objects
from credence.core.objects import SignedObject
from credence.core.primitives import KeyId
from credence.core.signatures import (
    KeyRegistry,
    KeyRole,
    KeyStatus,
    RegisteredKey,
    signature_message,
    verify_signed,
)

VECTORS = Path(__file__).resolve().parents[2] / "vectors" / "c3a"
MANIFEST = {name: sha for sha, name in json.loads((VECTORS / "MANIFEST.json").read_text())}
CLASSES = {cls.OBJECT_TYPE: cls for cls in objects.OBJECT_TYPES}


def load(name: str) -> dict[str, Any]:
    vector: dict[str, Any] = json.loads((VECTORS / name).read_text(encoding="ascii"))
    return vector


def by_hand(object_type: str, schema_version: str, digest: str) -> bytes:
    parts = ("credence/signature/v1", object_type, schema_version, digest)
    return b"".join(len(p).to_bytes(4, "big") + p.encode("ascii") for p in parts)


def test_manifest_matches_files() -> None:
    on_disk = {p.name for p in VECTORS.glob("*.json")} - {"MANIFEST.json"}
    assert on_disk == set(MANIFEST)
    for name, sha in MANIFEST.items():
        assert hashlib.sha256((VECTORS / name).read_bytes()).hexdigest() == sha


def test_regeneration_is_deterministic_and_matches_committed_files() -> None:
    path = VECTORS.parent / "build_c3a_signature_vectors.py"
    spec = importlib.util.spec_from_file_location("build_c3a_signature_vectors", path)
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    first, second = generator.render(), generator.render()
    assert first == second
    assert first == {p.name: p.read_text(encoding="ascii") for p in VECTORS.glob("*.json")}


@pytest.mark.parametrize(
    "required",
    [
        "purchase_request-signed",
        "delivery_receipt-gateway-signed",
        "provider_attestation-signed",
        "negative-wrong-key",
        "negative-altered-object",
        "revoked-key-facts",
        "negative-cross-type-replay",
    ],
)
def test_required_vector_present(required: str) -> None:
    assert f"{required}.json" in MANIFEST


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_vector(name: str) -> None:
    vector = load(name)
    expected = vector["expected"]
    registry = KeyRegistry(
        RegisteredKey(
            key_id=KeyId(entry["key_id"]),
            role=KeyRole(entry["role"]),
            subject=entry["subject"],
            status=KeyStatus(entry["status"]),
        )
        for entry in vector["registry"]
    )
    obj = CLASSES[vector["object"]["object_type"]].from_canonical(vector["object"])
    signed = SignedObject.from_canonical(vector["signed_object"])

    assert obj.digest().value == expected["object_digest"]
    message = bytes.fromhex(expected["message_hex"])
    assert message == signature_message(obj)
    assert message == by_hand(obj.OBJECT_TYPE, objects.SCHEMA_VERSION, expected["object_digest"])

    result = verify_signed(
        obj, signed, registry, role=KeyRole(vector["role"]), subject=vector["subject"]
    )
    assert result.signature_valid == expected["signature_valid"]
    assert result.key_current == expected["key_current"]
    assert (None if result.failure is None else result.failure.value) == expected["failure"]

    # Independent check with PyNaCl alone: the signature verifies over exactly this message
    # under exactly the key in key_id, or it does not.
    raw_ok = True
    try:
        VerifyKey(bytes.fromhex(signed.key_id.value.removeprefix("ed25519:"))).verify(
            message, bytes.fromhex(signed.signature.value)
        )
    except nacl.exceptions.BadSignatureError:
        raw_ok = False
    if expected["signature_valid"]:
        assert raw_ok
    if expected["failure"] == "BAD_SIGNATURE":
        assert not raw_ok
