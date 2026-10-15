"""Procurement matcher: structural requirements, null/not-asserted semantics, subset residency."""

from dataclasses import replace

import pytest
from c2_world import PROCUREMENT, PROFILE_B, SERVICE_B

from credence.core.primitives import (
    CountryCode,
    DeliveryMode,
    EvidenceTier,
    ProviderId,
)
from credence.core.procurement import (
    REQUIREMENTS,
    meets_tier,
    required_tier,
    structural_failures,
)

PROVIDER_B = ProviderId("provider-b")


def failures(**changes: object) -> tuple[str, ...]:
    profile_changes = {k: v for k, v in changes.items() if k in {"precision", "quantization"}}
    service_changes = {
        k: v for k, v in changes.items() if k not in set(profile_changes) | {"provider"}
    }
    provider = changes.get("provider", PROVIDER_B)
    return structural_failures(
        PROCUREMENT,
        replace(SERVICE_B, **service_changes),
        replace(PROFILE_B, **profile_changes),
        provider,  # type: ignore[arg-type]
    )


def test_base_service_matches() -> None:
    assert failures() == ()


@pytest.mark.parametrize(
    ("changes", "requirement"),
    [
        ({"model_family": "other"}, "model_family"),
        ({"precision": "FP8"}, "precision"),
        ({"precision": None}, "precision"),
        ({"quantization": "Q4_K_M"}, "quantization"),
        ({"quantization": None}, "quantization"),
        ({"delivery_mode": DeliveryMode.BATCH}, "delivery_mode"),
        ({"jurisdiction": CountryCode("FR")}, "jurisdiction"),
        ({"jurisdiction": None}, "jurisdiction"),
        ({"data_residency": ()}, "data_residency"),
        ({"provider": ProviderId("provider-z")}, "provider"),
    ],
)
def test_each_requirement(changes: dict[str, object], requirement: str) -> None:
    assert failures(**changes) == (requirement,)


def test_null_never_satisfies_even_when_everything_else_matches() -> None:
    assert failures(precision=None, quantization=None) == ("precision", "quantization")


def test_residency_is_subset_not_overlap() -> None:
    assert failures(data_residency=(CountryCode("DE"),)) == ()
    overlap = (CountryCode("DE"), CountryCode("JP"))  # DE allowed, JP not
    assert failures(data_residency=overlap) == ("data_residency",)


def test_no_precision_ranking() -> None:
    # FP32 is "more precise" than BF16 but is not in the allowed set, so it does not match.
    assert failures(precision="FP32") == ("precision",)


def test_failures_are_reported_in_fixed_order() -> None:
    every = failures(
        model_family="x",
        precision=None,
        quantization=None,
        delivery_mode=DeliveryMode.BATCH,
        jurisdiction=None,
        data_residency=(),
        provider=ProviderId("provider-z"),
    )
    assert every == REQUIREMENTS


def test_required_tier_is_the_stricter() -> None:
    assert required_tier(EvidenceTier.WITNESSED, PROCUREMENT) is EvidenceTier.WITNESSED
    strict = replace(PROCUREMENT, minimum_evidence_tier=EvidenceTier.MEASURED)
    assert required_tier(EvidenceTier.WITNESSED, strict) is EvidenceTier.MEASURED
    assert required_tier(EvidenceTier.BOUND_AND_SIGNED, PROCUREMENT) is (
        EvidenceTier.BOUND_AND_SIGNED
    )
    assert meets_tier(SERVICE_B, EvidenceTier.BOUND_AND_SIGNED)
    assert not meets_tier(SERVICE_B, EvidenceTier.MEASURED)
