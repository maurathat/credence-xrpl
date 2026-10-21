"""I2a: the agent independently verifies a prepared purchase before signing (credence.agent.client).

Every field the server returns is checked against what the agent intends; any tampering refuses
before a signature exists. Offline; keys generated per test.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_i2a_api import GENERAL, PROMPT, Demo, expect, no_network  # noqa: F401

from credence.agent.client import Expectations, VerificationFailed, verify_preparation
from credence.core.objects import PurchaseRequest
from credence.core.primitives import Sha256Digest, Timestamp
from credence.core.signatures import signature_message


def _prepared(tmp_path: Path) -> tuple[Demo, dict[str, Any]]:
    demo = Demo(tmp_path)
    return demo, demo.client(demo.a).prepare(GENERAL, PROMPT)


def _verify(demo: Demo, prepared: dict[str, Any], **kwargs: Any) -> PurchaseRequest:
    options: dict[str, Any] = {
        "key_id": demo.a.key_id,
        "prompt": PROMPT,
        "expect": expect(demo.a, demo),
        "now": demo.clock.peek(),
    }
    options.update(kwargs)
    return verify_preparation(prepared, **options)


def test_a_genuine_preparation_verifies(tmp_path: Path) -> None:
    demo, prepared = _prepared(tmp_path)
    request = _verify(demo, prepared)
    assert request.digest().value == prepared["request"]["digest"]


def _tamper(prepared: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    changed = copy.deepcopy(prepared)
    node = changed
    for name in path[:-1]:
        node = node[name]
    node[path[-1]] = value
    return changed


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("request", "digest"), "sha256:" + "0" * 64, "DIGEST_MISMATCH"),
        (("request", "canonical_json"), "{}", "CANONICAL_MISMATCH"),
        (("signing", "statement_hex"), "00", "STATEMENT_MISMATCH"),
        (("request", "object", "provider_id"), "local-qwen-provider", "DIGEST_MISMATCH"),
        (("provider_id"), "local-qwen-provider", "SUMMARY_MISMATCH"),
        (("nonce",), "0" * 32, "SUMMARY_MISMATCH"),
        (("agent_key_id",), "ed25519:" + "11" * 32, "WRONG_AGENT"),
        (("request", "object"), "not an object", "MALFORMED_PREPARATION"),
    ],
)
def test_a_tampered_preparation_is_refused(
    tmp_path: Path, path: tuple[str, ...] | str, value: Any, code: str
) -> None:
    demo, prepared = _prepared(tmp_path)
    keys = (path,) if isinstance(path, str) else path
    with pytest.raises(VerificationFailed) as refused:
        _verify(demo, _tamper(prepared, keys, value))
    assert refused.value.code == code


def test_a_consistently_rewritten_request_is_still_refused(tmp_path: Path) -> None:
    """A server that rewrites the request and every derived field consistently still fails the
    agent's own intent checks."""
    demo, prepared = _prepared(tmp_path)
    request = PurchaseRequest.from_canonical(prepared["request"]["object"])
    for change, code in (
        ({"expires_at": request.expires_at.plus_seconds(3600)}, "EXPIRY_TOO_LONG"),
        ({"prompt_digest": Sha256Digest("sha256:" + "ab" * 32)}, "PROMPT_MISMATCH"),
    ):
        rewritten = replace(request, **change)  # type: ignore[arg-type]
        forged = copy.deepcopy(prepared)
        forged["request"] = {
            "digest": rewritten.digest().value,
            "object": rewritten.to_canonical(),
            "canonical_json": rewritten.canonical_bytes().decode(),
        }
        forged["signing"]["statement_hex"] = signature_message(rewritten).hex()
        forged["expires_at"] = rewritten.expires_at.value
        with pytest.raises(VerificationFailed) as refused:
            _verify(demo, forged)
        assert refused.value.code == code


def test_the_agents_own_limits_apply(tmp_path: Path) -> None:
    demo, prepared = _prepared(tmp_path)
    facilities = expect(demo.a, demo).facility_ids
    price = prepared["price"]["max_total_cost_micro_trlusd"]
    for expectation, code in (
        (Expectations(facility_ids=frozenset({"elsewhere"})), "FACILITY_NOT_EXPECTED"),
        (
            Expectations(
                facility_ids=facilities, allowed_providers=frozenset({"local-qwen-provider"})
            ),
            "PROVIDER_NOT_ALLOWED",
        ),
        (
            Expectations(facility_ids=facilities, max_total_cost_micro_trlusd=price - 1),
            "PRICE_ABOVE_AGENT_CEILING",
        ),
        (Expectations(facility_ids=facilities, minimum_evidence_tier=2), "TIER_BELOW_AGENT_FLOOR"),
    ):
        with pytest.raises(VerificationFailed) as refused:
            _verify(demo, prepared, expect=expectation)
        assert refused.value.code == code
    with pytest.raises(VerificationFailed) as refused:
        _verify(demo, prepared, prompt=PROMPT + b"!")
    assert refused.value.code == "PROMPT_MISMATCH"
    with pytest.raises(VerificationFailed) as refused:
        _verify(demo, prepared, now=Timestamp(prepared["expires_at"]))
    assert refused.value.code == "EXPIRED"
    assert json.dumps(prepared).count(bytes(demo.a.key).hex()) == 0
