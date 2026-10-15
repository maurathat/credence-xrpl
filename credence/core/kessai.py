"""Kessai serializer binding for Credence Core.

Kessai serializer: JCS (RFC 8785) with NFC normalization, bound using the SCITT Canonical Payload
Binding `jcs` construction. Short form: JCS (RFC 8785) + NFC. NFC is not part of RFC 8785: it is
applied to every string value and member name before JCS serialization, so a plain JCS
implementation can produce different bytes for non-normalized input.

Authoritative implementation: `third_party/custody/impl/canon.py`, custody commit
5dc15ff6fb097dc5f4a47e72969fae4a296620a5, sha256 48d6ad1c92a292b1979ef05bc3010007ba0120e5da0d3be5f3f4f9b77902eb8d
(itself vendored unmodified from canonical-dataset-lineage, see custody's impl/CANON_PROVENANCE).
This module contains no canonicalization algorithm. Its public entry point `canon.dataset_id`
rejects every non-dataset shape, so, exactly as custody's own `impl/handoff.py` does for its
second payload class, it calls canon's pipeline steps directly:

    canon._admit       strict UTF-8, no BOM, well-formed JSON, no duplicate member names,
                       scalar Unicode, integral numbers within +/-(2**53 - 1)
    canon._fold_nfc    NFC-fold every string and member name, reject collisions
    canon._identifier  exclude a top-level `address`, RFC 8785 JCS, SHA-256 hex

canon.py does not return canonical bytes, so they are taken from `rfc8785.dumps` of the folded value,
the same call with the same pinned rfc8785 that canon._identifier makes, and are released only if
their SHA-256 equals canon._identifier's result.

Fail-closed additions, none of which change an admitted value:
- canon.py is loaded from the verified bytes; a hash or rfc8785 version mismatch refuses to load.
- Python floats are refused before encoding (canon admits `1.0` as the integer 1).
- The top level must be a JSON object, and a top-level `address` member is refused, because
  canon._identifier would silently leave it out of the digest.

Owner: Credence Core.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import cast

import rfc8785

type JsonValue = None | bool | int | str | list[JsonValue] | dict[str, JsonValue]

CUSTODY_COMMIT = "5dc15ff6fb097dc5f4a47e72969fae4a296620a5"
CANON_PATH = Path(__file__).resolve().parents[2] / "third_party" / "custody" / "impl" / "canon.py"
CANON_SHA256 = "48d6ad1c92a292b1979ef05bc3010007ba0120e5da0d3be5f3f4f9b77902eb8d"
RFC8785_VERSION = "0.1.4"
RESERVED_TOP_LEVEL_MEMBER = "address"


class SerializerUnavailable(RuntimeError):
    """The pinned serializer is missing or is not the pinned bytes. Nothing is serialized."""


class SerializationRejected(ValueError):
    """The serializer refused the input. `reason` is canon.py's reason code or an adapter code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class CanonicalForm:
    data: bytes
    sha256_hex: str


@cache
def _canon() -> ModuleType:
    if not CANON_PATH.is_file():
        raise SerializerUnavailable(
            f"{CANON_PATH} is missing: run `git submodule update --init third_party/custody`"
        )
    source = CANON_PATH.read_bytes()
    actual = hashlib.sha256(source).hexdigest()
    if actual != CANON_SHA256:
        raise SerializerUnavailable(f"canon.py sha256 {actual} != pinned {CANON_SHA256}")
    installed = importlib.metadata.version("rfc8785")
    if installed != RFC8785_VERSION:
        raise SerializerUnavailable(f"rfc8785 {installed} != pinned {RFC8785_VERSION}")
    module = ModuleType("credence.core._custody_canon")
    module.__file__ = str(CANON_PATH)
    exec(compile(source, str(CANON_PATH), "exec"), module.__dict__)  # the verified bytes only
    return module


def _admit_python(value: object) -> None:
    if value is None or isinstance(value, (bool, str)) or type(value) is int:
        return
    if isinstance(value, float):
        raise SerializationRejected("FLOAT_NOT_ADMITTED")
    if isinstance(value, list):
        for item in value:
            _admit_python(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SerializationRejected("NON_STRING_MEMBER_NAME")
            _admit_python(item)
        return
    raise SerializationRejected("UNSUPPORTED_TYPE")


def canonicalize_json_bytes(raw: bytes) -> CanonicalForm:
    """Canonical bytes and SHA-256 for a JSON text, through the pinned canon.py pipeline."""
    canon = _canon()
    reject = cast(type[Exception], canon.Reject)
    try:
        folded = canon._fold_nfc(canon._admit(raw))
    except reject as exc:
        raise SerializationRejected(str(exc)) from None  # canon.Reject's message is its reason
    if not isinstance(folded, dict):
        raise SerializationRejected("NOT_AN_OBJECT")
    if RESERVED_TOP_LEVEL_MEMBER in folded:
        raise SerializationRejected("ADDRESS_MEMBER_RESERVED")
    try:
        data = rfc8785.dumps(folded)
        identifier = cast(str, canon._identifier(folded))
    except rfc8785.CanonicalizationError:
        raise SerializationRejected("JCS_REJECTED") from None
    if hashlib.sha256(data).hexdigest() != identifier:
        raise SerializationRejected("DIGEST_MISMATCH")
    return CanonicalForm(data=data, sha256_hex=identifier)


def canonicalize(value: JsonValue) -> CanonicalForm:
    """Canonical bytes and SHA-256 for a JSON-shaped Python value (top level must be an object)."""
    _admit_python(value)
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return canonicalize_json_bytes(raw.encode("ascii"))
