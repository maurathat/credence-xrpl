"""Agent-signed authorization for response retrieval and acknowledgment (Integration I1e).

Knowing an operation ID and an agent's public key ID is not enough to read or delete a response.
The caller presents a `ResponseAuthorization`: an Ed25519 signature by the agent's registered key
over a domain-separated statement naming the protocol and version, the operation, the action,
the agent key, a fresh single-use nonce and a short expiry.

- Operational, not canonical: no frozen Core object or C3A signature definition is changed. The
  statement's domain string differs from C3A's `credence/signature/v1`, so an authorization can
  never be confused with a signature over a canonical object, or the reverse.
- The statement body is serialized with the Kessai serializer (JCS, RFC 8785, + NFC), the approved
  serialization, and signed with Ed25519, the approved primitive.
- Verification uses the current trusted key registry: the key must be registered, an `agent` key,
  `current` (not revoked), and the very key (and subject) that signed the operation's purchase.
- The nonce is consumed by the journal in the same transaction as the access it authorizes, so a
  replay fails across processes and restarts.

Nothing here logs or stores private keys, signatures for reuse, or response contents.

Owner: Integration (San Antonio).
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from enum import Enum

import nacl.exceptions
import nacl.signing

from credence.core.kessai import canonicalize
from credence.core.primitives import Ed25519Signature, KeyId, Timestamp
from credence.core.signatures import (
    KeyRegistry,
    KeyRole,
    KeyStatus,
    is_valid_public_key,
    key_id_for,
    public_key_bytes,
)

AUTHORIZATION_DOMAIN = "credence/integration/response-authorization/v1"
PROTOCOL = "credence.integration.response_authorization"
VERSION = "1"
MAX_LIFETIME_SECONDS = 300
_NONCE = re.compile(r"^[0-9a-f]{32}$")


class ResponseAction(Enum):
    RETRIEVE = "retrieve"
    ACKNOWLEDGE = "acknowledge"


class NotAuthorized(RuntimeError):
    """Access refused. `reason` is a code; no authorization material is included."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ResponseAuthorization:
    operation_id: str
    action: ResponseAction
    agent_key_id: KeyId
    nonce: str
    expires_at: Timestamp
    signature: Ed25519Signature


def _length_prefixed(text: str) -> bytes:
    data = text.encode("ascii")
    return len(data).to_bytes(4, "big") + data


def statement_bytes(
    operation_id: str,
    action: ResponseAction,
    agent_key_id: KeyId,
    nonce: str,
    expires_at: Timestamp,
) -> bytes:
    """The exact bytes signed: the domain, then the Kessai canonical form of the statement."""
    body = canonicalize(
        {
            "protocol": PROTOCOL,
            "version": VERSION,
            "operation_id": operation_id,
            "action": action.value,
            "agent_key_id": agent_key_id.value,
            "nonce": nonce,
            "expires_at": expires_at.value,
        }
    )
    return _length_prefixed(AUTHORIZATION_DOMAIN) + body.data


def sign_response_authorization(
    signing_key: nacl.signing.SigningKey,
    operation_id: str,
    action: ResponseAction,
    expires_at: Timestamp,
    *,
    nonce: str | None = None,
) -> ResponseAuthorization:
    """For the agent: authorize one action on one operation, once. The key is not retained."""
    nonce = secrets.token_hex(16) if nonce is None else nonce
    key_id = key_id_for(signing_key.verify_key)
    message = statement_bytes(operation_id, action, key_id, nonce, expires_at)
    return ResponseAuthorization(
        operation_id=operation_id,
        action=action,
        agent_key_id=key_id,
        nonce=nonce,
        expires_at=expires_at,
        signature=Ed25519Signature(signing_key.sign(message).signature.hex()),
    )


def verify_response_authorization(
    authorization: ResponseAuthorization,
    *,
    action: ResponseAction,
    operation_id: str,
    agent_key_id: str | None,
    agent_subject: str | None,
    registry: KeyRegistry,
    now: Timestamp,
) -> None:
    """Raise NotAuthorized unless `authorization` permits `action` on `operation_id` now.

    `agent_key_id` and `agent_subject` are those recorded when the operation's purchase request
    was signed. The nonce is checked for shape here and consumed by the journal."""
    if not isinstance(authorization, ResponseAuthorization):
        raise NotAuthorized("MALFORMED_AUTHORIZATION")
    if authorization.action is not action:
        raise NotAuthorized("WRONG_ACTION")
    if authorization.operation_id != operation_id:
        raise NotAuthorized("WRONG_OPERATION")
    if not _NONCE.match(authorization.nonce):
        raise NotAuthorized("MALFORMED_NONCE")
    if not now.value < authorization.expires_at.value:
        raise NotAuthorized("AUTHORIZATION_EXPIRED")
    if now.plus_seconds(MAX_LIFETIME_SECONDS).value < authorization.expires_at.value:
        raise NotAuthorized("AUTHORIZATION_TOO_LONG")
    key = registry.lookup(authorization.agent_key_id)
    if key is None or not is_valid_public_key(authorization.agent_key_id):
        raise NotAuthorized("UNKNOWN_KEY")
    message = statement_bytes(
        authorization.operation_id,
        authorization.action,
        authorization.agent_key_id,
        authorization.nonce,
        authorization.expires_at,
    )
    try:
        nacl.signing.VerifyKey(public_key_bytes(authorization.agent_key_id)).verify(
            message, bytes.fromhex(authorization.signature.value)
        )
    except nacl.exceptions.BadSignatureError:
        raise NotAuthorized("BAD_SIGNATURE") from None
    if key.role is not KeyRole.AGENT:
        raise NotAuthorized("WRONG_ROLE")
    if key.status is not KeyStatus.CURRENT:
        raise NotAuthorized("REVOKED_KEY")
    if agent_key_id is None or authorization.agent_key_id.value != agent_key_id:
        raise NotAuthorized("WRONG_AGENT")
    if agent_subject is None or key.subject != agent_subject:
        raise NotAuthorized("WRONG_AGENT")
