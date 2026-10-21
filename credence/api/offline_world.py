"""The I2a offline provider world for the embedded IntegrationService. OFFLINE MOCKS ONLY.

Nothing here calls a provider, a model or a network:

- Provider A is the real `NebiusProvider` adapter over `OfflineNebiusTransport`, a MOCK transport
  that returns a synthetic reply in the documented chat-completion shape. It is not a Nebius
  response and Nebius is never called.
- Provider B is the `FakeSigningProvider` standing in for local Qwen: it runs no model and signs a
  synthetic attestation with a TEST provider key.
- The gateway key and the mock provider key are TEST keys (NOT FOR PRODUCTION), derived at runtime
  from public labels so that nothing secret is stored or committed; only public key IDs leave
  this module.
- Quotes, service classes, execution profiles, procurement classes and facilities are the committed
  offline market configuration (contracts/fixtures/inference_c3/market-selection-deterministic.json),
  the configuration Product P0 shows. Quotes are configured DEMO prices in micro-tRLUSD (tRLUSD is a
  test token), never Nebius's billed price.

Agent keys are not here: each agent holds its own key; the operator registers only its public key ID.

Owner: API/UI (I2a integration). Provisional: subject to Inference and security review.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from nacl.signing import SigningKey

from credence.core.objects import (
    ExecutionProfile,
    Facility,
    ProcurementClass,
    Quote,
    ServiceClass,
)
from credence.core.primitives import KeyId, ProviderId
from credence.core.signatures import KeyRegistry, KeyRole, KeyStatus, RegisteredKey, key_id_for
from credence.inference.fake_providers import FakeSigningProvider
from credence.inference.nebius import NebiusProvider
from credence.inference.provider import InferenceProvider
from credence.integration.service import Market, Offer

ROOT = Path(__file__).resolve().parents[2]
MARKET_CONFIGURATION = (
    ROOT / "contracts" / "fixtures" / "inference_c3" / "market-selection-deterministic.json"
)
OFFLINE_KEY_PREFIX = b"CREDENCE I2A OFFLINE DEMO TEST KEY - NOT FOR PRODUCTION - "
GATEWAY_SUBJECT = "credence-gateway"
PROVIDER_LABEL = (
    "Offline mock provider (no provider call): synthetic responses, TEST keys, configured demo "
    "prices"
)

NEBIUS = "nebius-token-factory"
LOCAL = "local-qwen-provider"


def _test_key(label: str) -> SigningKey:
    """A TEST key (NOT FOR PRODUCTION) derived from a public label."""
    return SigningKey(hashlib.sha256(OFFLINE_KEY_PREFIX + label.encode("ascii")).digest())


GATEWAY_TEST_KEY = _test_key("gateway")
LOCAL_PROVIDER_TEST_KEY = _test_key("local-qwen-provider-mock")


class OfflineNebiusTransport:
    """OFFLINE MOCK of the Nebius HTTP transport: a synthetic reply in the documented
    non-streaming chat-completion shape. Never opens a connection. `posts` counts calls."""

    def __init__(self, reply: Mapping[str, object] | None = None) -> None:
        self.reply = reply
        self.posts: list[str] = []

    def get(self, path: str) -> Mapping[str, object]:
        raise RuntimeError("the offline mock transport serves no catalog")

    def post(self, path: str, body: Mapping[str, object]) -> Mapping[str, object]:
        self.posts.append(path)
        if self.reply is not None:
            return self.reply
        return {
            "id": "chatcmpl-offline-mock",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model"),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Synthetic offline mock reply (no provider was called).",
                    },
                    "finish_reason": "stop",
                    "logprobs": None,
                }
            ],
            "usage": {"prompt_tokens": 21, "completion_tokens": 9, "total_tokens": 30},
            "service_tier": "default",
        }


@dataclass(frozen=True)
class FacilityConfig:
    """An operator-configured facility and the procurement classes it allows."""

    facility: Facility
    procurement_classes: tuple[ProcurementClass, ...]
    title: str


@dataclass
class OfflineWorld:
    offers: tuple[Offer, ...]
    facilities: dict[str, FacilityConfig]
    nebius_transport: OfflineNebiusTransport
    nebius: NebiusProvider
    local: FakeSigningProvider

    def providers(self) -> tuple[InferenceProvider, ...]:
        return (self.nebius, self.local)

    def market(self, facility_id: str) -> Market:
        config = self.facilities[facility_id]
        return Market(config.facility, config.procurement_classes, self.offers)

    def registry(self, agents: Mapping[KeyId, str]) -> KeyRegistry:
        """The operator's registry: the gateway and mock provider TEST keys, and each agent's
        public key ID with the borrower account it speaks for."""
        keys = [
            RegisteredKey(
                key_id_for(GATEWAY_TEST_KEY.verify_key),
                KeyRole.GATEWAY,
                GATEWAY_SUBJECT,
                KeyStatus.CURRENT,
            ),
            RegisteredKey(
                key_id_for(LOCAL_PROVIDER_TEST_KEY.verify_key),
                KeyRole.PROVIDER,
                LOCAL,
                KeyStatus.CURRENT,
            ),
        ]
        keys += [
            RegisteredKey(key_id, KeyRole.AGENT, subject, KeyStatus.CURRENT)
            for key_id, subject in agents.items()
        ]
        return KeyRegistry(keys)


def offline_world(
    *,
    nebius_reply: Mapping[str, object] | None = None,
    extra_facilities: tuple[FacilityConfig, ...] = (),
) -> OfflineWorld:
    objects = json.loads(MARKET_CONFIGURATION.read_text(encoding="utf-8"))["objects"]
    offers: list[Offer] = []
    profiles: dict[str, ExecutionProfile] = {}
    for prefix, provider in (("nebius", NEBIUS), ("local", LOCAL)):
        profile = ExecutionProfile.from_canonical(objects[f"{prefix}_execution_profile"])
        offer = Offer(
            Quote.from_canonical(objects[f"{prefix}_quote"]),
            ServiceClass.from_canonical(objects[f"{prefix}_service_class"]),
            profile,
        )
        if offer.quote.provider_id.value != provider:
            raise ValueError(f"market configuration: {prefix} quote is not from {provider}")
        offers.append(offer)
        profiles[provider] = profile
    facilities = {}
    for name, title in (
        ("general", "Scenario A: general (Tier 1 floor)"),
        ("controlled", "Scenario B: controlled (Q8_0, Tier 2 floor)"),
    ):
        facility = Facility.from_canonical(objects[f"{name}_facility"])
        procurement = ProcurementClass.from_canonical(objects[f"{name}_procurement_class"])
        facilities[facility.facility_id.value] = FacilityConfig(facility, (procurement,), title)
    for extra in extra_facilities:
        facilities[extra.facility.facility_id.value] = extra
    transport = OfflineNebiusTransport(nebius_reply)
    return OfflineWorld(
        offers=tuple(offers),
        facilities=facilities,
        nebius_transport=transport,
        nebius=NebiusProvider(profiles[NEBIUS], transport=transport),
        local=FakeSigningProvider(
            profiles[LOCAL], LOCAL_PROVIDER_TEST_KEY, provider_id=ProviderId(LOCAL)
        ),
    )
