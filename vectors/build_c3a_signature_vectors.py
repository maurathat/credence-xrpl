"""Generate the Credence Core C3A signature vectors in vectors/c3a/.

Run from the repository root:  uv run python vectors/build_c3a_signature_vectors.py

Signed objects are the committed C1 vector objects. Signatures come from deterministic TEST keys
(NOT FOR PRODUCTION) whose 32-byte seeds are SHA-256 of a public label; no key material is
written, only public key IDs, messages and signatures. Ed25519 (RFC 8032) is deterministic, so
regeneration is byte-identical; tests/conformance/test_core_c3a_signature_vectors.py checks that
and re-verifies every vector, including with PyNaCl directly.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from nacl.signing import SigningKey

from credence.core.objects import (
    CanonicalObject,
    DeliveryReceiptBody,
    Facility,
    ProviderAttestationBody,
    PurchaseRequest,
    SignedObject,
)
from credence.core.primitives import Nonce
from credence.core.signatures import (
    KeyRegistry,
    KeyRole,
    KeyStatus,
    RegisteredKey,
    key_id_for,
    sign,
    signature_message,
    verify_signed,
)

HERE = Path(__file__).resolve().parent
OUT = HERE / "c3a"
TEST_KEY_PREFIX = b"CREDENCE TEST KEY - NOT FOR PRODUCTION - "


def c1(name: str) -> dict[str, Any]:
    value: dict[str, Any] = json.loads((HERE / "c1" / f"{name}.json").read_text())["input"]
    return value


def test_signing_key(label: str) -> SigningKey:
    return SigningKey(hashlib.sha256(TEST_KEY_PREFIX + label.encode("ascii")).digest())


REQUEST = PurchaseRequest.from_canonical(c1("purchase_request-basic"))
RECEIPT = DeliveryReceiptBody.from_canonical(c1("delivery_receipt-tier2"))
ATTESTATION = ProviderAttestationBody.from_canonical(c1("provider_attestation-basic"))
BORROWER = Facility.from_canonical(c1("facility-active")).borrower_account.value

KEYS: dict[str, tuple[KeyRole, str, KeyStatus]] = {
    "agent-working-key": (KeyRole.AGENT, BORROWER, KeyStatus.CURRENT),
    "agent-previous-working-key": (KeyRole.AGENT, BORROWER, KeyStatus.REVOKED),
    "credence-gateway": (KeyRole.GATEWAY, "credence-gateway", KeyStatus.CURRENT),
    "provider-a": (KeyRole.PROVIDER, "provider-a", KeyStatus.CURRENT),
    "provider-b": (KeyRole.PROVIDER, "provider-b", KeyStatus.CURRENT),
}
SIGNERS = {label: test_signing_key(label) for label in KEYS}


def kid(label: str) -> str:
    return key_id_for(SIGNERS[label].verify_key).value


REGISTRY_ENTRIES = [
    {"key_id": kid(label), "role": role.value, "subject": subject, "status": status.value}
    for label, (role, subject, status) in KEYS.items()
]


def registry() -> KeyRegistry:
    return KeyRegistry(
        RegisteredKey(
            key_id=key_id_for(SIGNERS[label].verify_key), role=role, subject=subject, status=status
        )
        for label, (role, subject, status) in KEYS.items()
    )


def vector(
    vid: str,
    description: str,
    obj: CanonicalObject,
    signed: SignedObject,
    role: KeyRole,
    subject: str | None,
    signer_label: str,
) -> dict[str, Any]:
    result = verify_signed(obj, signed, registry(), role=role, subject=subject)
    return {
        "id": vid,
        "description": description,
        "test_key_label": signer_label,
        "registry": REGISTRY_ENTRIES,
        "role": role.value,
        "subject": subject,
        "object": obj.to_canonical(),
        "signed_object": signed.to_canonical(),
        "expected": {
            "object_digest": obj.digest().value,
            "message_hex": signature_message(obj).hex(),
            "signature_valid": result.signature_valid,
            "key_current": result.key_current,
            "failure": None if result.failure is None else result.failure.value,
        },
    }


def build() -> list[dict[str, Any]]:
    request_signed = sign(REQUEST, SIGNERS["agent-working-key"])
    receipt_signed = sign(RECEIPT, SIGNERS["credence-gateway"])
    attestation_signed = sign(ATTESTATION, SIGNERS["provider-b"])
    altered = replace(REQUEST, nonce=Nonce("0" * 32))
    agent, gateway, provider = KeyRole.AGENT, KeyRole.GATEWAY, KeyRole.PROVIDER
    return [
        vector(
            "purchase_request-signed",
            "Purchase request signed by the agent's current working key",
            REQUEST, request_signed, agent, BORROWER, "agent-working-key",
        ),
        vector(
            "delivery_receipt-gateway-signed",
            "Tier 2 delivery receipt signed by the gateway",
            RECEIPT, receipt_signed, gateway, None, "credence-gateway",
        ),
        vector(
            "provider_attestation-signed",
            "Provider attestation signed by provider-b",
            ATTESTATION, attestation_signed, provider, "provider-b", "provider-b",
        ),
        vector(
            "negative-wrong-key",
            "Valid signature presented under another registered agent key",
            REQUEST,
            replace(request_signed, key_id=key_id_for(SIGNERS["agent-previous-working-key"].verify_key)),
            agent, BORROWER, "agent-working-key",
        ),
        vector(
            "negative-altered-object",
            "Request nonce changed after signing; signature retargeted to the new digest",
            altered, replace(request_signed, object_digest=altered.digest()),
            agent, BORROWER, "agent-working-key",
        ),
        vector(
            "revoked-key-facts",
            "Signed by a revoked agent key: signature valid, key not current (KEY_REVOKED)",
            REQUEST, sign(REQUEST, SIGNERS["agent-previous-working-key"]),
            agent, BORROWER, "agent-previous-working-key",
        ),
        vector(
            "negative-cross-type-replay",
            "Provider attestation signature replayed as the gateway's receipt signature",
            RECEIPT, replace(attestation_signed, object_digest=RECEIPT.digest()),
            gateway, None, "provider-b",
        ),
        vector(
            "negative-wrong-provider",
            "Attestation for provider-b signed by provider-a",
            ATTESTATION, sign(ATTESTATION, SIGNERS["provider-a"]),
            provider, "provider-b", "provider-a",
        ),
    ]  # fmt: skip


def render() -> dict[str, str]:
    files: dict[str, str] = {}
    manifest: list[list[str]] = []
    for item in build():
        text = json.dumps(item, ensure_ascii=True, indent=2) + "\n"
        name = f"{item['id']}.json"
        files[name] = text
        manifest.append([hashlib.sha256(text.encode("ascii")).hexdigest(), name])
    manifest.sort(key=lambda pair: pair[1])
    files["MANIFEST.json"] = json.dumps(manifest, indent=1) + "\n"
    return files


def main() -> int:
    files = render()
    OUT.mkdir(exist_ok=True)
    stale = sorted({p.name for p in OUT.glob("*.json")} - set(files))
    if stale:
        raise SystemExit(f"vectors/c3a has files the generator does not produce: {stale}")
    for name, text in files.items():
        (OUT / name).write_text(text, encoding="ascii")
    print(f"wrote {len(files) - 1} signature vectors to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
