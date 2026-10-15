"""Prompt and response payload digests, and where token counts come from.

Payload digest (contracts/INFERENCE-GATEWAY-CONTRACT.md, Prompt and response digests):

    payload_digest(data) = "sha256:" + lower-case hex SHA-256 of the exact bytes

The payload must be well-formed UTF-8 text. Nothing is normalized, wrapped or re-encoded first: no
NFC, no JSON, no Kessai serializer. A prompt or response is not a canonical Credence object, so its
digest identifies its exact bytes; changing any byte, including an NFC-equivalent rewrite, changes
the digest. Canonical objects keep only these digests, never the text.

Owner: Inference.
"""

from __future__ import annotations

import hashlib
from enum import Enum

from credence.core.primitives import Sha256Digest


class PayloadError(ValueError):
    """The payload is not bytes of well-formed UTF-8 text."""


def require_utf8(data: object) -> bytes:
    """Return `data` unchanged if it is `bytes` holding well-formed UTF-8; otherwise refuse."""
    if not isinstance(data, bytes):
        raise PayloadError(f"payload must be bytes, got {type(data).__name__}")
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PayloadError(f"payload is not well-formed UTF-8: {exc.reason}") from None
    return data


def payload_digest(data: bytes) -> Sha256Digest:
    """SHA-256 of the exact UTF-8 bytes of a prompt or response, as a `sha256:` digest."""
    return Sha256Digest.from_hex(hashlib.sha256(require_utf8(data)).hexdigest())


class TokenCountSource(Enum):
    """Who produced the token counts in a receipt.

    Checkpoint 1 has one source: the provider reports the counts and the gateway copies them. The
    gateway runs no tokenizer, so it does not verify them independently.
    """

    PROVIDER_REPORTED = "provider_reported"


GATEWAY_TOKEN_COUNT_SOURCE = TokenCountSource.PROVIDER_REPORTED
