"""Deterministic TEST keys for the C3A signature tests. NOT FOR PRODUCTION.

Each key's 32-byte seed is derived at runtime from a public label, so nothing secret is committed
and anyone can rebuild the same keys. Only public key IDs and signatures ever appear in files.
"""

import hashlib

from c2_world import FACILITY
from nacl.signing import SigningKey

from credence.core.primitives import KeyId
from credence.core.signatures import (
    KeyRegistry,
    KeyRole,
    KeyStatus,
    RegisteredKey,
    key_id_for,
)

TEST_KEY_PREFIX = b"CREDENCE TEST KEY - NOT FOR PRODUCTION - "


def make_test_key(label: str) -> SigningKey:
    return SigningKey(hashlib.sha256(TEST_KEY_PREFIX + label.encode("ascii")).digest())


AGENT = make_test_key("agent-working-key")
AGENT_REVOKED = make_test_key("agent-previous-working-key")
OTHER_AGENT = make_test_key("other-borrower-agent-key")
GATEWAY = make_test_key("credence-gateway")
GATEWAY_REVOKED = make_test_key("credence-gateway-retired")
PROVIDER_A = make_test_key("provider-a")
PROVIDER_B = make_test_key("provider-b")
PROVIDER_B_REVOKED = make_test_key("provider-b-retired")
ATTACKER = make_test_key("attacker-unregistered")

BORROWER = FACILITY.borrower_account.value
OTHER_BORROWER = "rrrrrrrrrrrrrrrrrrrrBZbvji"


def kid(key: SigningKey) -> KeyId:
    return key_id_for(key.verify_key)


def registry() -> KeyRegistry:
    return KeyRegistry(
        [
            RegisteredKey(kid(AGENT), KeyRole.AGENT, BORROWER, KeyStatus.CURRENT),
            RegisteredKey(kid(AGENT_REVOKED), KeyRole.AGENT, BORROWER, KeyStatus.REVOKED),
            RegisteredKey(kid(OTHER_AGENT), KeyRole.AGENT, OTHER_BORROWER, KeyStatus.CURRENT),
            RegisteredKey(kid(GATEWAY), KeyRole.GATEWAY, "credence-gateway", KeyStatus.CURRENT),
            RegisteredKey(
                kid(GATEWAY_REVOKED), KeyRole.GATEWAY, "credence-gateway", KeyStatus.REVOKED
            ),
            RegisteredKey(kid(PROVIDER_A), KeyRole.PROVIDER, "provider-a", KeyStatus.CURRENT),
            RegisteredKey(kid(PROVIDER_B), KeyRole.PROVIDER, "provider-b", KeyStatus.CURRENT),
            RegisteredKey(
                kid(PROVIDER_B_REVOKED), KeyRole.PROVIDER, "provider-b", KeyStatus.REVOKED
            ),
        ]
    )
