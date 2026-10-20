"""contracts/INFERENCE-GATEWAY-CONTRACT.md matches the implemented Inference Checkpoint 1 code."""

import dataclasses
import hashlib
import re
import typing
from pathlib import Path

from credence.core.objects import DeliveryReceiptBody
from credence.core.signatures import ROLE_OBJECT_TYPES, KeyRole
from credence.inference import gateway
from credence.inference.fake_providers import (
    FAKE_TIER1_PROVIDER_ID,
    FAKE_TIER2_PROVIDER_ID,
    FakeSigningProvider,
    FakeWitnessedProvider,
)
from credence.inference.gateway import GatewayRefusal, ProviderFailureKind
from credence.inference.payload import TokenCountSource, payload_digest
from credence.inference.provider import AttestationCapability, ProviderRequest, ProviderResult

TEXT = (
    Path(__file__).resolve().parents[2] / "contracts" / "INFERENCE-GATEWAY-CONTRACT.md"
).read_text()


def section(title: str) -> str:
    match = re.search(rf"^#+ {re.escape(title)}\n(.*?)(?=^#+ |\Z)", TEXT, re.M | re.S)
    assert match is not None, title
    return match.group(1)


def table_keys(title: str) -> list[str]:
    return re.findall(r"^\| `(\w+)` \|", section(title), re.M)


def listed(title: str, label: str) -> list[str]:
    match = re.search(rf"{re.escape(label)}: (.+?)\.\n", section(title))
    assert match is not None, label
    return re.findall(r"`(\w+)`", match.group(1))


def test_provider_request_fields() -> None:
    fields = [f.name for f in dataclasses.fields(ProviderRequest)]
    assert table_keys("Provider request boundary") == fields


def test_provider_result_fields() -> None:
    fields = [f.name for f in dataclasses.fields(ProviderResult)]
    assert table_keys("Provider response boundary") == fields


def test_attestation_capabilities() -> None:
    documented = listed("Provider response boundary", "`AttestationCapability` values")
    assert documented == [c.value for c in AttestationCapability]


def test_token_count_sources() -> None:
    documented = listed("Token accounting", "`TokenCountSource` values")
    assert documented == [s.value for s in TokenCountSource]


def test_payload_digest_rule() -> None:
    assert (
        '`payload_digest(data) = "sha256:" + lower-case hex SHA-256 of the exact bytes`'
        in section("Prompt and response digests")
    )
    data = "Synthetic café".encode()
    assert payload_digest(data).value == "sha256:" + hashlib.sha256(data).hexdigest()


def test_receipt_member_sources_cover_every_member() -> None:
    assert tuple(table_keys("DeliveryReceiptBody construction")) == DeliveryReceiptBody.MEMBERS


def test_outcomes() -> None:
    documented = table_keys("Outcomes")
    union = typing.get_args(gateway.GatewayOutcome.__value__)
    assert documented == [cls.__name__ for cls in union]


def test_failure_codes() -> None:
    behaviour = "Failure behaviour"
    assert listed(behaviour, "Gateway refusal codes") == [r.value for r in GatewayRefusal]
    assert listed(behaviour, "Provider failure kinds") == [k.value for k in ProviderFailureKind]


def test_signing_table_matches_c3a_roles() -> None:
    rows = re.findall(
        r"^\| [\w ]+ \| `([\w.]+)` \| `(\w+)` \|", section("Signing responsibilities"), re.M
    )
    assert {KeyRole(role): object_type for object_type, role in rows} == ROLE_OBJECT_TYPES


def test_fake_provider_table() -> None:
    rows = re.findall(r"^\| `(\w+)` \| `([\w-]+)` \| `(\w+)` \|", section("Fake providers"), re.M)
    assert rows == [
        (
            FakeWitnessedProvider.__name__,
            FAKE_TIER1_PROVIDER_ID.value,
            FakeWitnessedProvider.attestation_capability.value,
        ),
        (
            FakeSigningProvider.__name__,
            FAKE_TIER2_PROVIDER_ID.value,
            FakeSigningProvider.attestation_capability.value,
        ),
    ]


def test_tier_three_is_not_implemented() -> None:
    assert "| 3, Measured | Not implemented |" in section("Claim boundaries")


def test_local_provider_section_matches_code() -> None:
    from credence.inference import local_llama

    text = section("Local Tier 2 provider")
    for provider_id in (local_llama.LOCAL_PROVIDER_ID, local_llama.LOCAL_QWEN_PROVIDER_ID):
        assert f"| `{provider_id.value}` |" in text
    runtime = section("Runtime, template and tokenizer")
    assert local_llama.GGUF_FILE_TYPE_MOSTLY_F16 == 1
    assert "`general.file_type` is 1 (mostly F16)" in runtime
    assert local_llama.UNQUANTIZED_TENSOR_TYPES == {"F16", "F32"}
    assert "every tensor the runtime reports is F16 or F32" in runtime
    profile_rows = re.findall(
        r"^\| `(\w+)`(?:, `(\w+)`)? \|", section("Runtime, template and tokenizer"), re.M
    )
    documented = {name for row in profile_rows for name in row if name}
    from credence.core.objects import ExecutionProfile

    assert documented == set(ExecutionProfile.MEMBERS) - {
        "model_identifier",
        "model_artifact_digest",
    }
    assert "**Artifact binding is not execution measurement.**" in section("Claim boundary")


def test_unexpected_exceptions_are_documented_as_propagating() -> None:
    text = section("Failure behaviour")
    assert "- `PROVIDER_EXECUTION_ERROR`: the provider raised `ProviderExecutionError`." in text
    assert "it propagates to the caller" in text


def test_service_location_semantics_are_scoped_to_inference() -> None:
    text = section("Service location (jurisdiction and residency)")
    assert "Core leaves the general meaning of `ServiceClass.jurisdiction` undefined" in text
    assert "the country in which the inference execution for that service occurs" in text
    assert "It is not a Credence-wide or legal definition" in text
    assert "Not Nebius's legal or corporate jurisdiction" in text
    assert "operator-supplied deployment fact" in text


def test_template_and_q8_rules_match_the_binder() -> None:
    from credence.inference import local_llama

    runtime = section("Runtime, template and tokenizer")
    assert "byte-for-byte that file" in runtime and "`runtime_template_sha256`" in runtime
    assert local_llama.GGUF_FILE_TYPE_MOSTLY_Q8_0 == 7
    assert local_llama.Q8_0_TENSOR_TYPES == {"Q8_0", "F32"}
    assert "`general.file_type` is 7 (mostly Q8_0)" in runtime
    assert local_llama.asserted_representation(7, frozenset({"Q8_0", "F32"})) == (None, "Q8_0")


def test_nebius_section_matches_code() -> None:
    from credence.inference import nebius

    text = section("Nebius Tier 1 provider")
    assert f"provider ID `{nebius.NEBIUS_PROVIDER_ID.value}`" in text
    failures = section("Failures")
    documented = set(re.findall(r"`([a-z_]+)`", failures))
    assert {f.value for f in nebius.NebiusFailure} <= documented
    for failure in nebius.NebiusFailure:
        row = next(line for line in failures.splitlines() if f"`{failure.value}`" in line)
        expected = (
            "ProviderUnavailableError"
            if failure in nebius.UNAVAILABLE_FAILURES
            else "ProviderExecutionError"
        )
        assert expected in row, failure
    assert nebius.NEBIUS_BASE_URL in section("Credential boundary")


def test_market_rule_is_documented() -> None:
    text = section("Market routing")
    assert (
        "lowest maximum cost (Core's `max_total_cost_value` for the quote), then provider ID, then quote ID"
        in text
    )
    assert "never chosen, however cheap" in text
